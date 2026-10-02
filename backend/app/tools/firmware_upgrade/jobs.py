"""Upgrade jobs: the procedure engine.

A job is one device (or a pair, for an HA path). The platform driver supplies the
pre-checks and the procedure as a list of steps; this engine runs them one at a time,
records every check, event and backup, and remembers which step is next, so a job can
stop between steps and carry on later:

    planned -> pre-checks -> blocked | ready -> [stage image] -> Start
      Start: pre-checks again, then the steps: backup, copy + MD5 on the device,
             activate / reload, wait, post-checks (a gate), commit, backup ...
      a failed post-check stops the job (failed): fix and re-run the post-checks,
      override with a note, or roll back; then Continue carries on.

Steps that take a device down are only started inside the change window (live jobs),
and "stop after this step" is honoured before every step. A dry run runs the same
steps; anything that would change the device is only recorded.

The engine runs in the upgrade worker process (app.worker). The web app only queues
requests (request_action) and makes the quick database-only decisions (override,
cancel, stop).
"""

import hashlib
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.db import session_scope, utcnow
from app.core.models import Credential, Device
from app.core.ssh import DeviceError, Target, classify_error, run_commands, target_for
from app.tools.config_backup.models import BackupState
from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade import drivers
from app.tools.firmware_upgrade.deviceio import DeviceIO
from app.tools.firmware_upgrade.drivers.base import (ImageRef, Paused, Step, StepContext,
                                                     StepFailed, Unit, UpgradePath)
from app.tools.firmware_upgrade.models import (
    BLOCKED, BUSY_STATES, CANCELLED, COMPLETED, COMPLETED_OVERRIDES, FAILED, NEEDS_ATTENTION,
    PAUSED, PLANNED, POST_CHECKING, PRE_CHECKING, READY, ROLLED_BACK, ROLLING_BACK, RUNNING,
    STAGING, U_ACTIVATED, U_CHECKED, U_FAILED, U_ROLLED_BACK, DeviceUpgradeSettings,
    FirmwareImage, JobCheck, JobCircuits, JobEvent, JobRequest, JobSnapshot, JobUnit,
    UpgradeJob, WorkerStatus)

log = logging.getLogger(__name__)

ACTIONS = ("precheck", "stage", "start", "continue", "postcheck", "rollback")
ACTIVE_UNIT = (U_ACTIVATED, U_CHECKED, U_FAILED)  # units the procedure has changed
NO_OVERRIDE = {"image_md5", "image_platform", "device_md5", "device_size", "device_package"}
WORKER_STALE_SECONDS = 30


class JobError(Exception):
    pass


# --- records ----------------------------------------------------------------------------

def event(db: Session, job_id: int, message: str, step: str = "", level: str = "info",
          device_id: int | None = None) -> None:
    db.add(JobEvent(job_id=job_id, message=message, step=step, level=level, device_id=device_id))


def job_units(db: Session, job_id: int) -> list[JobUnit]:
    return db.scalars(select(JobUnit).where(JobUnit.job_id == job_id)
                      .order_by(JobUnit.position)).all()


def freeze_circuits(db: Session, job: UpgradeJob, stage: str) -> int:
    from app.tools.circuits.api import circuits_for_device  # the circuit list is optional data

    units = job_units(db, job.id) or [JobUnit(device_id=job.device_id)]
    groups, count, sources = [], 0, set()
    for ju in units:
        data = circuits_for_device(db, db.get(Device, ju.device_id))
        if data.get("import"):
            sources.add(data["import"]["filename"])
        count += data["count"]
        for g in data["groups"]:
            groups.append({**g, "service": f"{data['device']}: {g['service']}"} if len(units) > 1
                          else g)
    db.add(JobCircuits(job_id=job.id, stage=stage, source=", ".join(sorted(sources)),
                       count=count, data=json.dumps(groups, default=str)))
    return count


def _md5_file(path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.md5()  # noqa: S324 - the vendors publish MD5; SHA-512 is checked on upload
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def latest_checks(db: Session, job_id: int, phase: str) -> list[JobCheck]:
    """The newest run of a phase for each unit of the job."""
    rows = db.scalars(select(JobCheck).where(JobCheck.job_id == job_id, JobCheck.phase == phase)
                      .order_by(JobCheck.id)).all()
    newest: dict = {}
    for r in rows:
        newest[r.device_id] = max(newest.get(r.device_id, 0), r.run_no)
    return [r for r in rows if r.run_no == newest[r.device_id]]


def unresolved(checks: list[JobCheck]) -> list[JobCheck]:
    """Blockers that failed and haven't been overridden."""
    return [c for c in checks if c.severity == ck.BLOCKER and c.status in (ck.FAIL, ck.ERROR)
            and not c.overridden_by]


def has_overrides(db: Session, job_id: int) -> bool:
    return any(c.overridden_by for phase in ("pre", "post") for c in latest_checks(db, job_id, phase))


def load_plan(job: UpgradeJob) -> tuple[str, list[Step]]:
    data = json.loads(job.plan or "{}")
    if isinstance(data, list):
        data = {"mode": "upgrade", "steps": data}
    return data.get("mode", "upgrade"), [Step.from_dict(s) for s in data.get("steps", [])]


def in_window(job: UpgradeJob, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return bool(job.planned_start and job.planned_end and job.planned_start <= now <= job.planned_end)


def pending_request(db: Session, job_id: int) -> JobRequest | None:
    return db.scalar(select(JobRequest).where(JobRequest.job_id == job_id,
                                              JobRequest.finished_at.is_(None)).limit(1))


def worker_status(db: Session) -> dict:
    w = db.get(WorkerStatus, 1)
    if w is None:
        return {"alive": False, "beat_at": None, "job_id": None, "step": ""}
    alive = w.beat_at >= utcnow() - timedelta(seconds=WORKER_STALE_SECONDS)
    return {"alive": alive, "beat_at": w.beat_at, "pid": w.pid, "host": w.host,
            "started_at": w.started_at, "job_id": w.job_id if alive else None,
            "step": w.step if alive else ""}


def allowed_actions(db: Session, job: UpgradeJob, busy: bool) -> list[str]:
    if busy:
        return ["stop"] if job.status == RUNNING and not job.stop_requested else []
    s = job.status
    activated = any(u.status in ACTIVE_UNIT for u in job_units(db, job.id))
    mode, steps = load_plan(job)
    open_post = unresolved(latest_checks(db, job.id, "post"))
    acts = set()
    if s in (PLANNED, BLOCKED, READY) or (s == NEEDS_ATTENTION and not activated):
        acts.add("precheck")
    if s == READY:
        acts |= {"stage", "start"}
    if s == PAUSED or (s == FAILED and not open_post and job.step_index < len(steps)):
        acts.add("continue")
    if activated and s in (FAILED, PAUSED, COMPLETED_OVERRIDES, NEEDS_ATTENTION):
        acts.add("postcheck")
        if mode == "upgrade":
            acts.add("rollback")
    if s in (PLANNED, BLOCKED, READY, PAUSED, FAILED, NEEDS_ATTENTION):
        acts.add("cancel")
    return sorted(acts)


# --- quick actions from the web app (database only) -------------------------------------

def request_action(db: Session, settings: Settings, job: UpgradeJob, action: str,
                   username: str) -> JobRequest:
    """Queue an action for the upgrade worker, after checking it is allowed now."""
    if action not in ACTIONS:
        raise JobError(f"Unknown action {action}")
    if pending_request(db, job.id) or job.status in BUSY_STATES:
        raise JobError("This job is already running a step")
    if action not in allowed_actions(db, job, busy=False):
        raise JobError(f"Can't {action} a job that is {job.status.replace('_', ' ')}")
    if settings.upgrade_require_worker and not worker_status(db)["alive"]:
        raise JobError("The upgrade worker isn't running (service netops-worker), so nothing "
                       "can be run now")
    if action in ("start", "continue") and not job.dry_run:
        device = db.get(Device, job.device_id)
        if not settings.live_allowed(device.platform, job.path):
            raise JobError("Live upgrades are switched off for this platform "
                           "(NETOPS_UPGRADE_LIVE_PLATFORMS)")
        mode, steps = load_plan(job)
        reloads_left = action == "start" or any(s.reload for s in steps[job.step_index:])
        if mode == "upgrade" and reloads_left and not in_window(job):
            raise JobError("Outside the change window: a live upgrade can only be started or "
                           "continued between the planned start and end")
    req = JobRequest(job_id=job.id, action=action, username=username)
    db.add(req)
    event(db, job.id, f"{action.capitalize()} requested by {username}", action, "action")
    db.flush()
    return req


def request_stop(db: Session, job: UpgradeJob, username: str) -> None:
    if job.status != RUNNING:
        raise JobError("Only a running job can be stopped")
    job.stop_requested = True
    event(db, job.id, f"Stop after the current step requested by {username}", "stop", "action")


def override(db: Session, job: UpgradeJob, check: JobCheck, username: str, reason: str) -> None:
    if check.job_id != job.id:
        raise JobError("Check doesn't belong to this job")
    if check.check_id in NO_OVERRIDE:
        raise JobError("Checksum and platform checks can't be overridden - fix the image")
    phase_ok = {"pre": (BLOCKED, READY),
                "post": (FAILED, PAUSED, COMPLETED_OVERRIDES, NEEDS_ATTENTION)}.get(check.phase, ())
    if job.status not in phase_ok:
        raise JobError(f"A {check.phase}-check result can't be overridden while the job is "
                       f"{job.status.replace('_', ' ')}")
    latest = latest_checks(db, job.id, check.phase)
    if check not in latest:
        raise JobError("Only a result from the latest check run can be overridden")
    if check.status not in (ck.FAIL, ck.ERROR):
        raise JobError("Only a failed check can be overridden")
    check.overridden_by, check.override_reason, check.overridden_at = username, reason, utcnow()
    event(db, job.id, f"Override by {username} - {check.label}: {reason}", check.phase, "action",
          check.device_id)
    db.flush()
    remaining = unresolved(latest_checks(db, job.id, check.phase))
    if remaining:
        return
    if check.phase == "pre" and job.status == BLOCKED:
        job.status = READY
        event(db, job.id, "All pre-check blockers resolved or overridden - ready to start",
              "precheck")
    elif check.phase == "post" and job.status == FAILED:
        mode, steps = load_plan(job)
        if job.step_index < len(steps):
            job.status = PAUSED
            event(db, job.id, "All post-check failures overridden - Continue carries on with: "
                              f"{steps[job.step_index].label}", "postcheck")
        else:
            _finish(db, job, mode)


def cancel(db: Session, job: UpgradeJob, username: str, reason: str) -> None:
    if job.status in BUSY_STATES or pending_request(db, job.id):
        raise JobError("Wait for the running step to finish")
    if "cancel" not in allowed_actions(db, job, busy=False):
        raise JobError(f"Can't cancel a job that is {job.status.replace('_', ' ')}")
    changed = [db.get(Device, u.device_id).name for u in job_units(db, job.id)
               if u.status in ACTIVE_UNIT]
    job.status, job.finished_at, job.outcome_note = CANCELLED, utcnow(), reason
    event(db, job.id, f"Cancelled by {username}: {reason}" + (
        f" (already changed: {', '.join(changed)})" if changed else ""), "cancel", "action")


def _finish(db: Session, job: UpgradeJob, mode: str) -> None:
    if mode == "rollback":
        job.status = ROLLED_BACK
    else:
        job.status = COMPLETED_OVERRIDES if has_overrides(db, job.id) else COMPLETED
    job.finished_at, job.current_step = utcnow(), ""
    event(db, job.id, f"Job {job.status.replace('_', ' ')}", "done",
          "warn" if job.status == COMPLETED_OVERRIDES else "info")


# --- the context drivers work through -------------------------------------------------

class EngineContext(StepContext):
    def __init__(self, svc: "JobService", job: UpgradeJob, image: ImageRef, path: UpgradePath,
                 units: list[Unit], targets: dict):
        self.svc, self.job_id, self.dry_run = svc, job.id, job.dry_run
        self.image, self.path = image, path
        self.units = {u.device_id: u for u in units}
        self.pending_commit = job.pending_commit
        self.targets = targets  # device_id -> Target or an error message
        self._fdm: dict = {}

    def settings(self):
        return self.svc.settings

    def unit(self, device_id: int) -> Unit:
        return self.units[device_id]

    def target(self, u: Unit, long: bool = False) -> Target:
        t = self.targets.get(u.device_id)
        if not isinstance(t, Target):
            raise StepFailed(t or f"No upgrade account for {u.name}")
        return replace(t, command_timeout=self.svc.settings.upgrade_copy_timeout_min * 60) if long else t

    def event(self, message, step="", level="info", device_id=None):
        with session_scope() as db:
            event(db, self.job_id, message, step, level, device_id)

    def show(self, u, commands, long=False):
        try:
            return self.svc.io.show(self.target(u, long), list(commands))
        except StepFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StepFailed(f"{u.name}: {classify_error(exc)[1]}") from None

    def change(self, u, commands, *, config=False, expect_reload=False, long=False):
        text = "; ".join(commands)
        if self.dry_run:
            self.event(f"DRY RUN - would {'configure' if config else 'run'} on {u.name}: {text}",
                       "change", "action", u.device_id)
            return ""
        self.event(f"{'Configuring' if config else 'Running'} on {u.name}: {text}", "change",
                   "action", u.device_id)
        s = self.svc.settings
        timeout = s.upgrade_copy_timeout_min * 60 if long else s.command_timeout
        try:
            out = self.svc.io.change(self.target(u, long), list(commands), config=config,
                                     timeout=timeout, expect_reload=expect_reload)
        except StepFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StepFailed(f"'{text}' on {u.name}: {classify_error(exc)[1]}",
                             attention=expect_reload) from None
        tail = "\n".join(line for line in (out or "").splitlines() if line.strip())[-1500:]
        if tail:
            self.event(f"Output from {u.name}:\n{tail}", "change", "info", u.device_id)
        return out or ""

    def transfer(self, u, file_system):
        mb = self.image.size // 2**20
        if self.dry_run:
            self.event(f"DRY RUN - would copy {self.image.filename} ({mb} MB) to {u.name} "
                       f"{file_system} over SCP", "stage", "action", u.device_id)
            return
        self.event(f"Copying {self.image.filename} ({mb} MB) to {u.name} {file_system}",
                   "stage", "action", u.device_id)
        started = self.svc.monotonic()
        try:
            result = self.svc.io.transfer(self.target(u, long=True), self.image.path, file_system,
                                          self.image.filename,
                                          self.svc.settings.upgrade_copy_timeout_min * 60)
        except StepFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StepFailed(f"Copy to {u.name} failed: {classify_error(exc)[1]}") from None
        self.event(f"{self.image.filename} {result} on {u.name} "
                   f"({(self.svc.monotonic() - started) / 60:.1f} min)", "stage", "info", u.device_id)

    def poll(self, fn, minutes: float, what: str) -> bool:
        deadline = self.svc.monotonic() + minutes * 60
        while True:
            if fn():
                return True
            if self.svc.monotonic() >= deadline:
                return False
            self.svc.sleep(self.svc.settings.upgrade_poll_seconds)

    def settle(self, u):
        secs = self.svc.settings.upgrade_settle_seconds
        if secs:
            self.event(f"Waiting {secs} s for {u.name} to settle", "wait", "info", u.device_id)
            self.svc.sleep(secs)

    def wait_back(self, u, expect_down=True):
        s = self.svc.settings
        if self.dry_run:
            self.event(f"DRY RUN - would wait up to {s.upgrade_reload_timeout_min} min for "
                       f"{u.name} to reload and come back", "wait", "action", u.device_id)
            return
        t = self.target(u)
        started = self.svc.monotonic()
        if expect_down and not self.poll(lambda: not self.svc.io.reachable(t), 10,
                                         f"{u.name} to go down"):
            raise StepFailed(f"{u.name} didn't go down for its reload within 10 minutes",
                             attention=True)
        self.event(f"{u.name} is reloading - waiting for it to come back", "wait", "info",
                   u.device_id)

        def back() -> bool:
            if not self.svc.io.reachable(t):
                return False
            try:
                self.svc.io.show(t, ["show version"])
                return True
            except Exception:  # noqa: BLE001 - still booting
                return False

        if not self.poll(back, s.upgrade_reload_timeout_min, f"{u.name} to come back"):
            raise StepFailed(f"{u.name} didn't come back within {s.upgrade_reload_timeout_min} "
                             "minutes - check it on the console", attention=True)
        self.event(f"{u.name} is back after {(self.svc.monotonic() - started) / 60:.1f} min",
                   "wait", "info", u.device_id)
        self.settle(u)

    def fdm(self, u):
        if u.device_id not in self._fdm:
            client = self.svc.io.fdm(self.target(u), u.fdm_fingerprint)
            client.login()
            self._fdm[u.device_id] = client
        return self._fdm[u.device_id]

    def forget_fdm(self, u):
        self._fdm.pop(u.device_id, None)

    def fdm_change(self, u, label, fn):
        if self.dry_run:
            self.event(f"DRY RUN - would {label} on {u.name} (FDM API)", "change", "action",
                       u.device_id)
            return None
        self.event(f"FDM API on {u.name}: {label}", "change", "action", u.device_id)
        try:
            return fn(self.fdm(u))
        except DeviceError as exc:
            raise StepFailed(f"{u.name}: {exc}") from None

    def record_checks(self, u, phase, results):
        with session_scope() as db:
            job = db.get(UpgradeJob, self.job_id)
            run_no = {"pre": job.pre_run, "post": job.post_run}.get(phase, job.stage_run)
            return self.svc._store_checks(db, job, phase, run_no, results, u.device_id)

    def set_job(self, **fields):
        if self.dry_run:  # nothing was activated, so there is no timer to watch
            if "abort_minutes" in fields:
                self.event(f"DRY RUN - the device would revert by itself after "
                           f"{fields['abort_minutes']} min unless committed", "activate", "action")
            return
        with session_scope() as db:
            job = db.get(UpgradeJob, self.job_id)
            if "pending_commit" in fields:
                job.pending_commit = self.pending_commit = fields["pending_commit"]
                if not fields["pending_commit"]:
                    job.abort_deadline = None
            if "abort_minutes" in fields:
                job.abort_deadline = utcnow() + timedelta(minutes=fields["abort_minutes"])
                event(db, self.job_id, "Not committed yet: the device reverts by itself at "
                      f"{job.abort_deadline:%H:%M} UTC unless the new version is committed",
                      "activate", "warn")


# --- the engine ------------------------------------------------------------------------------

class JobService:
    def __init__(self, settings: Settings, cipher: CredentialCipher, backup_service,
                 io: DeviceIO | None = None, collector=run_commands,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.settings = settings
        self.cipher = cipher
        self.backup = backup_service
        self.io = io or DeviceIO(collector)
        self.sleep, self.monotonic = sleep, monotonic
        self.stopping = False  # set by the worker on shutdown: pause at the next step
        self.on_step = lambda job_id, label: None  # the worker's heartbeat hook

    # --- dispatch (the worker calls this) ------------------------------------------------

    def run(self, job_id: int, action: str, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            if job is None:
                return "job not found"
            if action not in allowed_actions(db, job, busy=False):
                event(db, job_id, f"{action} not carried out: the job is now "
                                  f"{job.status.replace('_', ' ')}", action, "warn")
                return f"refused ({job.status})"
        fn = {"precheck": self.precheck, "stage": self.stage, "start": self.start,
              "continue": self.continue_, "postcheck": self.postcheck,
              "rollback": self.rollback}[action]
        try:
            return fn(job_id, username)
        except Exception as exc:  # noqa: BLE001 - record, never lose a job silently
            log.exception("Upgrade job %s %s crashed", job_id, action)
            with session_scope() as db:
                job = db.get(UpgradeJob, job_id)
                job.status = NEEDS_ATTENTION
                event(db, job_id, f"{action} stopped by an internal error: {exc}", action, "error")
            return NEEDS_ATTENTION

    def recover_interrupted(self) -> None:
        """At worker start: nothing is resumed blindly."""
        with session_scope() as db:
            for job in db.scalars(select(UpgradeJob).where(UpgradeJob.status.in_(BUSY_STATES))):
                prev = job.status
                job.status = NEEDS_ATTENTION
                event(db, job.id, f"The upgrade worker restarted while the job was {prev}"
                      + (f" ({job.current_step})" if job.current_step else "")
                      + ": check the device, then re-run the checks", "recover", "error")
            now = utcnow()
            for req in db.scalars(select(JobRequest).where(JobRequest.finished_at.is_(None))):
                req.finished_at = now
                if req.claimed_at:
                    req.result = "interrupted by a worker restart"
                elif req.requested_at < now - timedelta(minutes=5):
                    req.result = "expired: the worker wasn't running"
                    event(db, req.job_id, f"{req.action} requested by {req.username} was not "
                                          "carried out: the upgrade worker wasn't running",
                          req.action, "warn")
                else:
                    req.finished_at = None  # recent: still run it

    def watch_abort_timers(self) -> None:
        """IOS-XE: tell the engineer when an uncommitted upgrade has reverted by itself."""
        with session_scope() as db:
            for job in db.scalars(select(UpgradeJob).where(UpgradeJob.pending_commit.is_(True))):
                if job.abort_deadline and job.abort_deadline < utcnow() and \
                        job.status not in BUSY_STATES:
                    job.pending_commit, job.status = False, NEEDS_ATTENTION
                    event(db, job.id, "The auto-abort timer ran out before the new version was "
                          "committed: the device has gone back to the previous version by itself. "
                          "Re-run the post-checks to confirm.", "activate", "error")

    # --- building blocks -------------------------------------------------------------------

    def _image(self, db: Session, job: UpgradeJob) -> ImageRef:
        img = db.get(FirmwareImage, job.image_id)
        return ImageRef(img.id, img.filename, img.version, img.md5, img.size,
                        self.settings.firmware_dir / img.filename)

    def _units(self, db: Session, job: UpgradeJob) -> list[Unit]:
        units = []
        for ju in job_units(db, job.id):
            d = db.get(Device, ju.device_id)
            s = db.get(DeviceUpgradeSettings, d.id)
            units.append(Unit(device_id=d.id, name=d.name, address=d.address, platform=d.platform,
                              position=ju.position, role=ju.role, status=ju.status,
                              from_version=ju.from_version, running_image=ju.running_image,
                              rollback_ref=ju.rollback_ref, staged=ju.staged,
                              image_present=ju.image_present,
                              file_system=s.file_system if s else "",
                              fdm_fingerprint=s.fdm_fingerprint if s else ""))
        return units

    def _save_units(self, db: Session, job_id: int, units) -> None:
        for u in units:
            ju = db.scalar(select(JobUnit).where(JobUnit.job_id == job_id,
                                                 JobUnit.device_id == u.device_id))
            ju.role, ju.status, ju.from_version = u.role, u.status, u.from_version
            ju.running_image, ju.rollback_ref = u.running_image, u.rollback_ref
            ju.staged, ju.image_present = u.staged, u.image_present

    def _targets(self, db: Session, units: list[Unit]) -> dict:
        targets = {}
        for u in units:
            s = db.get(DeviceUpgradeSettings, u.device_id)
            cred = db.get(Credential, s.upgrade_credential_id) if s and s.upgrade_credential_id else None
            if cred is None:
                targets[u.device_id] = f"No upgrade account is set for {u.name}"
                continue
            try:
                targets[u.device_id] = target_for(db.get(Device, u.device_id), self.cipher,
                                                  self.settings.ssh_timeout,
                                                  self.settings.command_timeout, credential=cred)
            except Exception as exc:  # noqa: BLE001
                targets[u.device_id] = classify_error(exc)[1]
        return targets

    def _context(self, db: Session, job: UpgradeJob) -> tuple[EngineContext, "drivers.UpgradeDriver"]:
        driver = drivers.for_platform(db.get(Device, job.device_id).platform)
        units = self._units(db, job)
        path = driver.path(job.path) or driver.paths[0]
        return EngineContext(self, job, self._image(db, job), path, units,
                             self._targets(db, units)), driver

    def _collect(self, ctx: EngineContext, driver, u: Unit) -> tuple[dict | None, str]:
        commands = {**ck.commands_for(u.platform), **driver.extra_commands(u, ctx.image)}
        t = ctx.targets.get(u.device_id)
        if not isinstance(t, Target):
            return None, t
        try:
            outputs = self.io.show(t, list(commands.values()))
        except Exception as exc:  # noqa: BLE001
            return None, classify_error(exc)[1]
        return dict(zip(commands.keys(), outputs)), ""

    def _thresholds(self) -> ck.Thresholds:
        return ck.Thresholds(self.settings.upgrade_cpu_warn, self.settings.upgrade_mem_warn)

    def _backup_check(self, device_id: int, job_id: int) -> ck.CheckResult:
        """A config backup within the allowed age; take one now if needed."""
        max_age = timedelta(hours=self.settings.upgrade_backup_max_age_hours)
        with session_scope() as db:
            st = db.get(BackupState, device_id)
            if st and st.last_success and st.last_success >= utcnow() - max_age:
                return ck.CheckResult("config_backup", "Recent config backup", ck.BLOCKER,
                                      ck.PASS, f"{st.last_success:%Y-%m-%d %H:%M} UTC")
            event(db, job_id, "No recent config backup - taking one now", "precheck",
                  device_id=device_id)
        self.backup.run(device_id, f"upgrade-job:{job_id}")
        with session_scope() as db:
            st = db.get(BackupState, device_id)
            ok = st and st.last_status == "success"
            return ck.CheckResult("config_backup", "Recent config backup", ck.BLOCKER,
                                  ck.PASS if ok else ck.FAIL,
                                  f"taken now ({st.last_commit[:8] if ok and st.last_commit else ''})"
                                  if ok else "backup failed",
                                  "" if ok else (st.last_error if st else "no backup state"))

    def _store_checks(self, db: Session, job: UpgradeJob, phase: str, run_no: int,
                      results: list[ck.CheckResult], device_id: int | None) -> list[JobCheck]:
        """Store results. An earlier override is kept only if the same check on the same
        device failed in exactly the same way (e.g. the same accepted alarm). Post-checks
        also inherit overrides given at pre-check for an identical condition."""
        def key(check_id, value, detail):
            return check_id, detail, "" if check_id in ("cpu", "memory") else value

        earlier = [c for c in latest_checks(db, job.id, phase) if c.device_id == device_id]
        if phase == "post":
            earlier += [c for c in latest_checks(db, job.id, "pre") if c.device_id == device_id]
        previous = {key(c.check_id, c.value, c.detail): c for c in earlier if c.overridden_by}
        rows = []
        for r in results:
            row = JobCheck(job_id=job.id, device_id=device_id, phase=phase, run_no=run_no,
                           **r.as_dict())
            k = key(r.check_id, r.value, r.detail)
            if r.status in (ck.FAIL, ck.ERROR) and k in previous and r.check_id not in NO_OVERRIDE:
                p = previous[k]
                carried = " (carried over from the pre-check)" if p.phase != phase else ""
                row.overridden_by = p.overridden_by
                row.override_reason = (p.override_reason or "") + (
                    carried if carried not in (p.override_reason or "") else "")
                row.overridden_at = p.overridden_at
            db.add(row)
            rows.append(row)
        db.flush()
        return rows

    def _snapshot(self, db: Session, job_id: int, device_id: int, phase: str = "pre") -> dict:
        snap = db.scalar(select(JobSnapshot).where(
            JobSnapshot.job_id == job_id, JobSnapshot.device_id == device_id,
            JobSnapshot.phase == phase).order_by(JobSnapshot.id.desc()).limit(1))
        return json.loads(snap.data) if snap else {}

    # --- actions -----------------------------------------------------------------------------

    def precheck(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status, job.pre_run = PRE_CHECKING, job.pre_run + 1
            run_no = job.pre_run
            event(db, job_id, f"Pre-checks started by {username} (run {run_no})", "precheck",
                  "action")
            ctx, driver = self._context(db, job)
            img = db.get(FirmwareImage, job.image_id)
            image_facts = ck.ImageFacts(platform=img.platform, version=img.version,
                                        model_pattern=img.model_pattern, size=img.size,
                                        md5_ok=None, filename=img.filename)
        md5 = _md5_file(ctx.image.path)
        image_facts.md5_ok = None if md5 is None else (md5 == ctx.image.md5)

        per_unit, snaps, all_logged_in = {}, {}, True
        for u in ctx.units.values():
            t = ctx.targets.get(u.device_id)
            has_cred = not (isinstance(t, str) and t.startswith("No upgrade account"))
            results = [ck.CheckResult("upgrade_credential", "Upgrade account set", ck.BLOCKER,
                                      ck.PASS if has_cred else ck.FAIL,
                                      t.username if isinstance(t, Target) else "",
                                      "" if has_cred else "An admin sets it in the device's "
                                                          "upgrade settings")]
            outputs, error = self._collect(ctx, driver, u)
            results.append(ck.CheckResult("login", f"Log in to {u.name}", ck.BLOCKER,
                                          ck.PASS if outputs else ck.FAIL, "", error))
            if outputs:
                device_results, facts = ck.pre_checks(
                    u.platform, outputs, image_facts, self._thresholds(),
                    self.settings.upgrade_flash_factor, self.settings.upgrade_flash_factor_install)
                u.facts = facts
                u.from_version = facts.get("version") or u.from_version
                results += device_results
                try:
                    results += driver.extra_prechecks(ctx, u, outputs, ctx.path)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Driver pre-checks failed")
                    results.append(ck.CheckResult("platform_checks", "Platform checks",
                                                  ck.BLOCKER, ck.ERROR, "", str(exc)))
                snaps[u.device_id] = ck.snapshot(u.platform, outputs)
            else:
                all_logged_in = False
                results += ck.image_checks(u.platform, image_facts)
            results.append(self._backup_check(u.device_id, job_id))
            per_unit[u.device_id] = results
        if ctx.path.units == 2 and all_logged_in:
            first = min(ctx.units.values(), key=lambda u: u.position)
            per_unit[first.device_id] += driver.pair_checks(ctx, list(ctx.units.values()), ctx.path)

        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            rows = []
            for device_id, results in per_unit.items():
                rows += self._store_checks(db, job, "pre", run_no, results, device_id)
                if device_id in snaps:
                    db.add(JobSnapshot(job_id=job_id, device_id=device_id, phase="pre",
                                       run_no=run_no, data=json.dumps(snaps[device_id])))
            self._save_units(db, job_id, ctx.units.values())
            main = next(u for u in ctx.units.values() if u.position == 0)
            if main.from_version and job.status == PRE_CHECKING:
                job.from_version = main.from_version
            open_blockers = unresolved(rows)
            job.status = BLOCKED if open_blockers else READY
            event(db, job_id, "Pre-checks: " + (
                f"{len(open_blockers)} blocker(s): " + ", ".join(c.label for c in open_blockers)
                if open_blockers else "ready to start"), "precheck",
                "warn" if open_blockers else "info")
            return job.status

    def stage(self, job_id: int, username: str) -> str:
        """Copy the image and check it on the device, without any reload."""
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status = STAGING
            event(db, job_id, f"Staging started by {username}", "stage", "action")
            ctx, driver = self._context(db, job)
        problem = ""
        for u in driver.order_units(list(ctx.units.values())):
            try:
                self._bump_stage_run(job_id)
                driver.step_stage(ctx, Step(f"stage:{u.device_id}", "stage", "stage",
                                            u.device_id), u)
            except StepFailed as exc:
                problem = str(exc)
                ctx.event(f"Staging failed: {exc}", "stage", "error", u.device_id)
                break
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            self._save_units(db, job_id, ctx.units.values())
            job.status = READY
            if not problem and all(u.staged for u in ctx.units.values()):
                job.staged_at = utcnow()
                event(db, job_id, "Image staged and verified on every unit", "stage")
            elif not problem:
                event(db, job_id, "Staging finished (dry run: nothing copied)", "stage")
            return READY

    def _bump_stage_run(self, job_id: int) -> None:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.stage_run += 1

    def start(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            device = db.get(Device, job.device_id)
            if not job.dry_run:
                why = ("live upgrades are switched off for this platform"
                       if not self.settings.live_allowed(device.platform, job.path) else
                       "it is outside the change window" if not in_window(job) else "")
                if why:
                    event(db, job_id, f"Not started: {why}", "start", "warn")
                    return job.status
            event(db, job_id, f"Start by {username} ({'DRY RUN' if job.dry_run else 'LIVE'}) - "
                              "re-running the pre-checks first", "start", "action")
        if self.precheck(job_id, username) != READY:
            with session_scope() as db:
                event(db, job_id, "Not started: the pre-checks found blockers", "start", "warn")
            return BLOCKED
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            ctx, driver = self._context(db, job)
            ordered = driver.order_units(list(ctx.units.values()))
            steps = driver.upgrade_plan(ctx, ctx.path, ordered)
            job.plan = json.dumps({"mode": "upgrade", "steps": [s.as_dict() for s in steps]})
            job.step_index, job.stop_requested = 0, False
            job.status, job.started_by, job.started_at = RUNNING, username, utcnow()
            n = freeze_circuits(db, job, "started")
            event(db, job_id, f"{n} affected circuit(s) recorded at start", "start")
            event(db, job_id, f"Procedure ({ctx.path.label}), {len(steps)} steps: "
                  + " → ".join(s.label for s in steps), "start")
        return self._execute(job_id)

    def continue_(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            mode, steps = load_plan(job)
            job.status = ROLLING_BACK if mode == "rollback" else RUNNING
            job.stop_requested = False
            event(db, job_id, f"Continued by {username}: next step "
                  f"{steps[job.step_index].label if job.step_index < len(steps) else '-'}",
                  "continue", "action")
        return self._execute(job_id)

    def rollback(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            ctx, driver = self._context(db, job)
            steps = driver.rollback_plan(ctx, ctx.path, driver.order_units(list(ctx.units.values())))
            if not steps:
                event(db, job_id, "Nothing to roll back: no unit was changed", "rollback", "warn")
                return job.status
            old = json.loads(job.plan or "{}")
            job.plan = json.dumps({"mode": "rollback", "steps": [s.as_dict() for s in steps],
                                   "upgrade": old.get("steps", old) if isinstance(old, dict) else old})
            job.step_index, job.stop_requested, job.status = 0, False, ROLLING_BACK
            event(db, job_id, f"Roll back by {username} ({'DRY RUN' if job.dry_run else 'LIVE'}), "
                  f"{len(steps)} steps: " + " → ".join(s.label for s in steps), "rollback", "action")
        return self._execute(job_id)

    def postcheck(self, job_id: int, username: str) -> str:
        """Re-run the post-checks on every unit the procedure has changed."""
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            mode, steps = load_plan(job)
            job.status = POST_CHECKING
            event(db, job_id, f"Post-checks re-run by {username}", "postcheck", "action")
            ctx, driver = self._context(db, job)
        changed = [u for u in ctx.units.values() if u.status in ACTIVE_UNIT + (U_ROLLED_BACK,)]
        failures, passed = [], []
        for u in changed:
            open_ = self._postcheck_unit(ctx, driver, u, "from" if mode == "rollback" else None, mode)
            failures += open_
            if not open_:
                passed.append(u.device_id)
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            self._save_units(db, job_id, ctx.units.values())
            if failures:
                job.status = FAILED
                return FAILED
            # The units are fine: move past their post-check step if the job stopped before it
            for i in range(job.step_index, len(steps)):
                if steps[i].gate and steps[i].device_id in passed:
                    job.step_index = i + 1
                    break
            if job.step_index < len(steps):
                job.status = PAUSED
                event(db, job_id, f"Post-checks passed - Continue carries on with: "
                                  f"{steps[job.step_index].label}", "postcheck")
            else:
                _finish(db, job, mode)
            return job.status

    def _postcheck_unit(self, ctx: EngineContext, driver, u: Unit, target: str | None,
                        mode: str) -> list[JobCheck]:
        with session_scope() as db:
            job = db.get(UpgradeJob, ctx.job_id)
            job.post_run += 1
            run_no = job.post_run
            pre = self._snapshot(db, job.id, u.device_id)
            target_version = job.target_version
            if target == "from" or ctx.dry_run:
                target_version = u.from_version or job.from_version
                if ctx.dry_run and target != "from":
                    event(db, job.id, f"DRY RUN - post-checks on {u.name} compare against the "
                          f"current version ({target_version}) because nothing was installed",
                          "postcheck", device_id=u.device_id)
        outputs, error = self._collect(ctx, driver, u)
        results = [ck.CheckResult("post_login", f"Log in to {u.name}", ck.BLOCKER,
                                  ck.PASS if outputs else ck.FAIL, "", error)]
        post = {}
        if outputs:
            results += ck.health_checks(u.platform, outputs, self._thresholds())
            post = ck.snapshot(u.platform, outputs)
            results += ck.compare_snapshots(pre, post, target_version)
        with session_scope() as db:
            job = db.get(UpgradeJob, ctx.job_id)
            rows = self._store_checks(db, job, "post", run_no, results, u.device_id)
            if post:
                db.add(JobSnapshot(job_id=job.id, device_id=u.device_id, phase="post",
                                   run_no=run_no, data=json.dumps(post)))
            open_ = unresolved(rows)
            if open_:
                u.status = U_FAILED
            else:
                u.status = U_ROLLED_BACK if mode == "rollback" else U_CHECKED
            event(db, job.id, f"Post-checks on {u.name}: " + (
                f"{len(open_)} failure(s): " + ", ".join(c.label for c in open_)
                if open_ else "all passed"), "postcheck", "warn" if open_ else "info", u.device_id)
            return open_

    def _backup_step(self, ctx: EngineContext, step: Step, u: Unit) -> None:
        label = step.args.get("label", "")
        self.backup.run(u.device_id, f"upgrade-job:{ctx.job_id}:{label}")
        with session_scope() as db:
            job = db.get(UpgradeJob, ctx.job_id)
            st = db.get(BackupState, u.device_id)
            ok = bool(st and st.last_status == "success")
            commit = (st.last_commit or "") if ok else ""
            if label == "before" and job.pre_backup_commit is None:
                job.pre_backup_commit = commit or None
            elif label != "before":
                job.post_backup_commit = commit or job.post_backup_commit
            event(db, ctx.job_id, f"Config backup ({label}) of {u.name}: "
                  + (commit[:8] if ok else f"failed - {st.last_error if st else ''}"),
                  "backup", "info" if ok else ("error" if label == "before" else "warn"), u.device_id)
        if label == "before" and not ok:
            raise StepFailed(f"The config backup of {u.name} before the upgrade failed")

    def _run_step(self, ctx: EngineContext, driver, step: Step, mode: str) -> list:
        u = ctx.units.get(step.device_id)
        if step.kind == "backup":
            self._backup_step(ctx, step, u)
            return []
        if step.kind == "postcheck":
            return self._postcheck_unit(ctx, driver, u, step.args.get("target"), mode)
        if step.kind == "stage":
            self._bump_stage_run(ctx.job_id)
        fn = getattr(driver, f"step_{step.kind}", None)
        if fn is None:
            raise StepFailed(f"The {driver.platform} driver has no '{step.kind}' step")
        fn(ctx, step, u)
        return []

    def _execute(self, job_id: int) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            ctx, driver = self._context(db, job)
        while True:
            with session_scope() as db:
                job = db.get(UpgradeJob, job_id)
                mode, steps = load_plan(job)
                idx = job.step_index
                if idx >= len(steps):
                    _finish(db, job, mode)
                    return job.status
                step = steps[idx]
                if mode == "upgrade":
                    why = ("stop requested" if job.stop_requested else
                           "the upgrade worker is shutting down" if self.stopping else
                           "the change window has ended" if step.reload and not job.dry_run
                           and not in_window(job) else "")
                    if why:
                        job.status, job.current_step = PAUSED, ""
                        event(db, job_id, f"Paused before '{step.label}': {why}. Continue "
                                          "carries on from here.", "pause", "warn")
                        return PAUSED
                job.current_step = step.label
                event(db, job_id, f"Step {idx + 1}/{len(steps)}: {step.label}", step.kind,
                      "action", step.device_id)
            self.on_step(job_id, step.label)
            try:
                failures = self._run_step(ctx, driver, step, mode)
            except Paused as exc:
                return self._stop_at(job_id, ctx, PAUSED, str(exc), "warn")
            except StepFailed as exc:
                return self._stop_at(job_id, ctx, NEEDS_ATTENTION if exc.attention else FAILED,
                                     f"{step.label} failed: {exc}", "error")
            except Exception as exc:  # noqa: BLE001
                log.exception("Step %s of job %s crashed", step.id, job_id)
                return self._stop_at(job_id, ctx, NEEDS_ATTENTION,
                                     f"{step.label} stopped by an internal error: {exc}", "error")
            with session_scope() as db:
                job = db.get(UpgradeJob, job_id)
                self._save_units(db, job_id, ctx.units.values())
                job.step_index = idx + 1
                if failures:
                    job.status, job.current_step = FAILED, ""
                    event(db, job_id, "Stopped: post-check failure(s). Fix and re-run the "
                          "post-checks, override with a note, or roll back.", "postcheck", "warn")
                    return FAILED

    def _stop_at(self, job_id: int, ctx: EngineContext, status: str, message: str,
                 level: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            self._save_units(db, job_id, ctx.units.values())
            job.status, job.current_step = status, ""
            event(db, job_id, message, "step", level)
        return status
