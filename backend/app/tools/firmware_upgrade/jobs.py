"""Upgrade jobs: one device per job, a fixed procedure, and a full record.

    planned -> (pre-check) -> blocked | ready -> (Start) -> running -> post-checks
            -> completed | completed_overrides | failed -> (re-check / override / roll back)

This release runs every job as a DRY RUN: pre-checks and post-checks really log in
and read the device, but the upgrade steps (copy, verify, activate, reload) are only
recorded as "would ..." events. Nothing on the device is changed.
"""

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.db import session_scope, utcnow
from app.core.models import Credential, Device
from app.core.ssh import Target, classify_error, run_commands, target_for
from app.tools.config_backup.models import BackupState
from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.models import (BLOCKED, BUSY_STATES, CANCELLED, COMPLETED,
                                               COMPLETED_OVERRIDES, FAILED, NEEDS_ATTENTION,
                                               PLANNED, POST_CHECKING, PRE_CHECKING, READY,
                                               ROLLED_BACK, ROLLING_BACK, RUNNING,
                                               DeviceUpgradeSettings, FirmwareImage, JobCheck,
                                               JobCircuits, JobEvent, JobSnapshot, UpgradeJob)

log = logging.getLogger(__name__)

Collector = Callable[[Target, list[str]], list[str]]

# Which actions each status allows. Overrides are handled in override().
ALLOWED = {
    "precheck": {PLANNED, BLOCKED, READY, NEEDS_ATTENTION},
    "start": {READY},
    "postcheck": {FAILED, COMPLETED_OVERRIDES, NEEDS_ATTENTION},
    "rollback": {FAILED, COMPLETED_OVERRIDES, NEEDS_ATTENTION},
    "cancel": {PLANNED, BLOCKED, READY},
}


class JobError(Exception):
    pass


def event(db: Session, job_id: int, message: str, step: str = "", level: str = "info") -> None:
    db.add(JobEvent(job_id=job_id, message=message, step=step, level=level))


def freeze_circuits(db: Session, job: UpgradeJob, stage: str) -> int:
    from app.tools.circuits.api import circuits_for_device  # circuits tool is optional data

    data = circuits_for_device(db, db.get(Device, job.device_id))
    source = data["import"]["filename"] if data.get("import") else ""
    db.add(JobCircuits(job_id=job.id, stage=stage, source=source, count=data["count"],
                       data=json.dumps(data["groups"], default=str)))
    return data["count"]


def _md5_file(path) -> bool | None:
    if not path.exists():
        return None
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def _latest(db: Session, job_id: int, phase: str, run_no: int) -> list[JobCheck]:
    return db.scalars(select(JobCheck).where(JobCheck.job_id == job_id, JobCheck.phase == phase,
                                             JobCheck.run_no == run_no)
                      .order_by(JobCheck.id)).all()


def unresolved(checks: list[JobCheck]) -> list[JobCheck]:
    """Blockers that failed and haven't been overridden."""
    return [c for c in checks if c.severity == ck.BLOCKER and c.status in (ck.FAIL, ck.ERROR)
            and not c.overridden_by]


class JobService:
    def __init__(self, settings: Settings, cipher: CredentialCipher, backup_service,
                 collector: Collector = run_commands):
        self.settings = settings
        self.cipher = cipher
        self.backup = backup_service
        self.collector = collector
        self._pool = ThreadPoolExecutor(max_workers=settings.upgrade_workers,
                                        thread_name_prefix="upgrade")
        self._busy: set[int] = set()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- queueing -------------------------------------------------------------

    def submit(self, job_id: int, action: str, username: str) -> None:
        """Validate and queue an action; raises JobError if not allowed now."""
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            if job is None:
                raise JobError("Job not found")
            if job.status not in ALLOWED[action]:
                raise JobError(f"Can't {action} a job that is {job.status.replace('_', ' ')}")
        with self._lock:
            if job_id in self._busy:
                raise JobError("This job is already running a step")
            self._busy.add(job_id)
        self._pool.submit(self._run, job_id, action, username)

    def is_busy(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._busy

    def _run(self, job_id: int, action: str, username: str) -> None:
        try:
            {"precheck": self.precheck, "start": self.start, "postcheck": self.postcheck,
             "rollback": self.rollback}[action](job_id, username)
        except Exception as exc:  # noqa: BLE001 - record, never lose a job silently
            log.exception("Upgrade job %s %s crashed", job_id, action)
            with session_scope() as db:
                job = db.get(UpgradeJob, job_id)
                if job:
                    job.status = NEEDS_ATTENTION
                    event(db, job_id, f"{action} stopped by an internal error: {exc}",
                          action, "error")
        finally:
            with self._lock:
                self._busy.discard(job_id)

    def recover_interrupted(self) -> None:
        with session_scope() as db:
            for job in db.scalars(select(UpgradeJob).where(UpgradeJob.status.in_(BUSY_STATES))):
                prev = job.status
                job.status = NEEDS_ATTENTION
                event(db, job.id, f"The service restarted while the job was {prev}; "
                                  "check the device, then re-run the checks", "recover", "error")

    # --- device access ----------------------------------------------------------

    def _context(self, db: Session, job: UpgradeJob):
        device = db.get(Device, job.device_id)
        image = db.get(FirmwareImage, job.image_id)
        settings = db.get(DeviceUpgradeSettings, job.device_id)
        cred = db.get(Credential, settings.upgrade_credential_id) if (
            settings and settings.upgrade_credential_id) else None
        target, setup_error = None, None
        if cred is None:
            setup_error = "No upgrade credential is set for this device"
        else:
            try:
                target = target_for(device, self.cipher, self.settings.ssh_timeout,
                                    self.settings.command_timeout, credential=cred)
            except Exception as exc:  # noqa: BLE001
                setup_error = classify_error(exc)[1]
        return device, image, cred, target, setup_error

    def _collect(self, target: Target, platform: str) -> tuple[dict[str, str] | None, str]:
        commands = ck.commands_for(platform)
        try:
            outputs = self.collector(target, list(commands.values()))
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
            fresh = st and st.last_success and st.last_success >= utcnow() - max_age
            if fresh:
                return ck.CheckResult("config_backup", "Recent config backup", ck.BLOCKER,
                                      ck.PASS, f"{st.last_success:%Y-%m-%d %H:%M} UTC")
            event(db, job_id, "No recent config backup - taking one now", "precheck")
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
                      results: list[ck.CheckResult]) -> list[JobCheck]:
        """Store results. An earlier override is kept only if the same check failed in
        exactly the same way (same detail), e.g. the same accepted alarm. Post-checks
        also inherit overrides given at pre-check for an identical condition."""
        def key(check_id, value, detail):
            return check_id, detail, "" if check_id in ("cpu", "memory") else value

        earlier = []
        if run_no > 1:
            earlier += _latest(db, job.id, phase, run_no - 1)
        if phase == "post" and job.pre_run:
            earlier += _latest(db, job.id, "pre", job.pre_run)
        previous = {key(c.check_id, c.value, c.detail): c for c in earlier if c.overridden_by}
        rows = []
        for r in results:
            row = JobCheck(job_id=job.id, phase=phase, run_no=run_no, **r.as_dict())
            k = key(r.check_id, r.value, r.detail)
            if r.status in (ck.FAIL, ck.ERROR) and k in previous:
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

    # --- actions ----------------------------------------------------------------

    def precheck(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status, job.pre_run = PRE_CHECKING, job.pre_run + 1
            run_no = job.pre_run
            event(db, job_id, f"Pre-checks started by {username} (run {run_no})", "precheck",
                  "action")
            device, image, cred, target, setup_error = self._context(db, job)
            platform, device_id = device.platform, device.id
            img = ck.ImageFacts(platform=image.platform, version=image.version,
                                model_pattern=image.model_pattern, size=image.size,
                                md5_ok=None, filename=image.filename)
            image_md5, image_path = image.md5, self.settings.firmware_dir / image.filename
            cred_name = cred.name if cred else ""

        md5 = _md5_file(image_path)
        img.md5_ok = None if md5 is None else (md5 == image_md5)
        results = [ck.CheckResult("upgrade_credential", "Upgrade credential set", ck.BLOCKER,
                                  ck.PASS if cred_name else ck.FAIL, cred_name,
                                  "" if cred_name else "Set it in the job or the device's firmware settings")]
        outputs, error = (None, setup_error) if setup_error else self._collect(target, platform)
        results.append(ck.CheckResult("login", "Log in to the device", ck.BLOCKER,
                                      ck.PASS if outputs else ck.FAIL, "", error))
        snap = {}
        if outputs:
            device_results, _facts = ck.pre_checks(
                platform, outputs, img, self._thresholds(), self.settings.upgrade_flash_factor,
                self.settings.upgrade_flash_factor_install)
            results += device_results
            snap = ck.snapshot(platform, outputs)
        else:
            results += ck.image_checks(platform, img)
        results.append(self._backup_check(device_id, job_id))

        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            rows = self._store_checks(db, job, "pre", run_no, results)
            if snap:
                db.add(JobSnapshot(job_id=job_id, phase="pre", run_no=run_no,
                                   data=json.dumps(snap)))
                if not job.from_version:
                    job.from_version = snap.get("version") or ""
            open_blockers = unresolved(rows)
            job.status = BLOCKED if open_blockers else READY
            event(db, job_id, "Pre-checks: " + (
                f"{len(open_blockers)} blocker(s): " + ", ".join(c.label for c in open_blockers)
                if open_blockers else "ready to start"), "precheck",
                "warn" if open_blockers else "info")
            return job.status

    def start(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            event(db, job_id, f"Start requested by {username} - re-running pre-checks first",
                  "start", "action")
        if self.precheck(job_id, username) != READY:
            with session_scope() as db:
                event(db, job_id, "Not started: pre-checks found blockers", "start", "warn")
            return BLOCKED

        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status, job.started_by, job.started_at = RUNNING, username, utcnow()
            n = freeze_circuits(db, job, "started")
            event(db, job_id, f"{n} affected circuit(s) recorded at start", "start")
            device_id = job.device_id
        self.backup.run(device_id, f"upgrade-job:{job_id}:before")
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            st = db.get(BackupState, device_id)
            job.pre_backup_commit = st.last_commit if st else None
            event(db, job_id, f"Config backup before the upgrade: "
                              f"{(job.pre_backup_commit or 'failed')[:8]}", "backup")
            device, image = db.get(Device, device_id), db.get(FirmwareImage, job.image_id)
            pre_snap = self._snapshot(db, job, "pre", job.pre_run)
            for step, text in ck.upgrade_steps(device.platform, pre_snap.get("ios_xe", False),
                                               image.filename, image.md5, image.size,
                                               job.from_version):
                event(db, job_id, f"DRY RUN - would: {text}", step, "action")
        return self.postcheck(job_id, username, after_start=True)

    def postcheck(self, job_id: int, username: str, after_start: bool = False,
                  target_version: str | None = None, final_status: str | None = None) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status, job.post_run = POST_CHECKING, job.post_run + 1
            run_no = job.post_run
            if not after_start:
                event(db, job_id, f"Post-checks re-run by {username} (run {run_no})",
                      "postcheck", "action")
            device, image, cred, target, setup_error = self._context(db, job)
            platform, device_id = device.platform, device.id
            pre_snap = self._snapshot(db, job, "pre", job.pre_run)
            target_version = target_version or job.target_version
            if job.dry_run and target_version == job.target_version:
                # Nothing was installed, so compare against the version still running.
                target_version = job.from_version or target_version
                event(db, job_id, "DRY RUN - post-checks compare against the current version "
                                  f"({target_version}) because nothing was installed",
                      "postcheck")

        outputs, error = (None, setup_error) if setup_error else self._collect(target, platform)
        results = [ck.CheckResult("post_login", "Log in after the upgrade", ck.BLOCKER,
                                  ck.PASS if outputs else ck.FAIL, "", error)]
        post_snap = {}
        if outputs:
            results += ck.health_checks(platform, outputs, self._thresholds())
            post_snap = ck.snapshot(platform, outputs)
            results += ck.compare_snapshots(pre_snap, post_snap, target_version)
        self.backup.run(device_id, f"upgrade-job:{job_id}:after")

        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            rows = self._store_checks(db, job, "post", run_no, results)
            if post_snap:
                db.add(JobSnapshot(job_id=job_id, phase="post", run_no=run_no,
                                   data=json.dumps(post_snap)))
            st = db.get(BackupState, device_id)
            job.post_backup_commit = st.last_commit if st else None
            open_failures = unresolved(rows)
            if final_status:
                job.status = final_status
            elif open_failures:
                job.status = FAILED
            else:
                any_override = any(c.overridden_by for c in rows) or any(
                    c.overridden_by for c in _latest(db, job_id, "pre", job.pre_run))
                job.status = COMPLETED_OVERRIDES if any_override else COMPLETED
            if job.status != FAILED:
                job.finished_at = utcnow()
            event(db, job_id, "Post-checks: " + (
                f"{len(open_failures)} failure(s): " + ", ".join(c.label for c in open_failures)
                if open_failures else "all passed") + f" - job {job.status.replace('_', ' ')}",
                "postcheck", "warn" if open_failures else "info")
            return job.status

    def rollback(self, job_id: int, username: str) -> str:
        with session_scope() as db:
            job = db.get(UpgradeJob, job_id)
            job.status = ROLLING_BACK
            event(db, job_id, f"Roll back requested by {username}", "rollback", "action")
            device = db.get(Device, job.device_id)
            pre_snap = self._snapshot(db, job, "pre", job.pre_run)
            for step, text in ck.rollback_steps(device.platform, pre_snap.get("ios_xe", False),
                                                job.from_version):
                event(db, job_id, f"DRY RUN - would: {text}", step, "action")
            from_version = job.from_version
        return self.postcheck(job_id, username, after_start=True, target_version=from_version,
                              final_status=ROLLED_BACK)

    def _snapshot(self, db: Session, job: UpgradeJob, phase: str, run_no: int) -> dict:
        snap = db.scalar(select(JobSnapshot).where(
            JobSnapshot.job_id == job.id, JobSnapshot.phase == phase,
            JobSnapshot.run_no == run_no))
        return json.loads(snap.data) if snap else {}


# --- synchronous actions used by the API -------------------------------------------

def override(db: Session, job: UpgradeJob, check: JobCheck, username: str, reason: str) -> None:
    if check.job_id != job.id:
        raise JobError("Check doesn't belong to this job")
    phase_ok = {"pre": (BLOCKED, READY), "post": (FAILED, COMPLETED_OVERRIDES)}[check.phase]
    latest = job.pre_run if check.phase == "pre" else job.post_run
    if job.status not in phase_ok or check.run_no != latest:
        raise JobError("Only a result from the latest check run of a blocked or failed job "
                       "can be overridden")
    if check.status not in (ck.FAIL, ck.ERROR):
        raise JobError("Only a failed check can be overridden")
    check.overridden_by, check.override_reason, check.overridden_at = username, reason, utcnow()
    event(db, job.id, f"Override by {username} - {check.label}: {reason}", check.phase, "action")
    db.flush()
    remaining = unresolved(_latest(db, job.id, check.phase, latest))
    if check.phase == "pre" and not remaining:
        job.status = READY
        event(db, job.id, "All pre-check blockers resolved or overridden - ready to start",
              "precheck")
    elif check.phase == "post" and not remaining and job.status == FAILED:
        job.status, job.finished_at = COMPLETED_OVERRIDES, utcnow()
        event(db, job.id, "All post-check failures overridden - job completed with overrides",
              "postcheck")


def cancel(db: Session, job: UpgradeJob, username: str, reason: str) -> None:
    if job.status not in ALLOWED["cancel"]:
        raise JobError(f"Can't cancel a job that is {job.status.replace('_', ' ')}")
    job.status, job.finished_at, job.outcome_note = CANCELLED, utcnow(), reason
    event(db, job.id, f"Cancelled by {username}: {reason}", "cancel", "action")
