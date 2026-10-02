"""HTTP API for upgrade jobs, per-device upgrade settings and repository details."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import User, current_user, require_admin, require_upgrader
from app.core.db import get_db
from app.core.models import Credential, Device
from app.core.platforms import PLATFORMS
from app.core.ssh import DeviceError
from app.tools.firmware_upgrade import drivers
from app.tools.firmware_upgrade import jobs as jb
from app.tools.firmware_upgrade.facts import pattern_matches, vendor_for
from app.tools.firmware_upgrade.models import (BUSY_STATES, CLOSED_STATES, DeviceFacts,
                                               DeviceUpgradeSettings, FirmwareImage, ImageInfo,
                                               JobCheck, JobCircuits, JobEvent, JobUnit,
                                               UpgradeJob)
from app.tools.firmware_upgrade.report import render_csv, render_html

router = APIRouter(prefix="/api/firmware", tags=["upgrade jobs"])


def _settings(request: Request):
    return request.app.state.settings


def _check_out(c: JobCheck, names: dict) -> dict:
    return {"id": c.id, "run_no": c.run_no, "device_id": c.device_id,
            "device": names.get(c.device_id, ""), "check_id": c.check_id, "label": c.label,
            "severity": c.severity, "status": c.status, "value": c.value, "detail": c.detail,
            "overridden_by": c.overridden_by, "override_reason": c.override_reason,
            "overridden_at": c.overridden_at, "overridable": c.check_id not in jb.NO_OVERRIDE}


def _circuits_out(row: JobCircuits | None) -> dict | None:
    if row is None:
        return None
    return {"count": row.count, "source": row.source, "created_at": row.created_at,
            "groups": json.loads(row.data)}


def _upgrade_cred(db: Session, device_id: int) -> Credential | None:
    s = db.get(DeviceUpgradeSettings, device_id)
    return db.get(Credential, s.upgrade_credential_id) if s and s.upgrade_credential_id else None


def _device_out(d: Device) -> dict:
    return {"id": d.id, "name": d.name, "address": d.address, "platform": d.platform,
            "platform_label": PLATFORMS[d.platform].label if d.platform in PLATFORMS
            else d.platform, "site": d.site}


def job_summary(db: Session, job: UpgradeJob, settings) -> dict:
    d = db.get(Device, job.device_id)
    img = db.get(FirmwareImage, job.image_id)
    req = jb.pending_request(db, job.id)
    busy = req is not None or job.status in BUSY_STATES
    driver = drivers.for_platform(d.platform)
    path = driver.path(job.path) if driver else None
    mode, steps = jb.load_plan(job)
    units = []
    for ju in jb.job_units(db, job.id):
        ud = db.get(Device, ju.device_id)
        cred = _upgrade_cred(db, ju.device_id)
        units.append({"device_id": ud.id, "name": ud.name, "address": ud.address,
                      "position": ju.position, "role": ju.role, "status": ju.status,
                      "from_version": ju.from_version, "staged": ju.staged,
                      "upgrade_credential": cred.name if cred else None})
    return {
        "id": job.id, "status": job.status, "busy": busy, "dry_run": job.dry_run,
        "queued": req.action if req is not None and req.claimed_at is None else None,
        "device": _device_out(d),
        "image": {"id": img.id, "filename": img.filename, "version": img.version, "md5": img.md5,
                  "size": img.size, "vendor": vendor_for(img.platform)},
        "path": job.path, "path_label": path.label if path else job.path,
        "live_allowed": settings.live_allowed(d.platform, job.path),
        "in_window": jb.in_window(job), "units": units,
        "from_version": job.from_version, "target_version": job.target_version,
        "change_ref": job.change_ref, "notes": job.notes,
        "planned_start": job.planned_start, "planned_end": job.planned_end,
        "created_by": job.created_by, "created_at": job.created_at,
        "started_by": job.started_by, "started_at": job.started_at,
        "finished_at": job.finished_at, "outcome_note": job.outcome_note,
        "pre_backup_commit": job.pre_backup_commit, "post_backup_commit": job.post_backup_commit,
        "pre_run": job.pre_run, "post_run": job.post_run,
        "mode": mode, "step_index": job.step_index, "current_step": job.current_step,
        "steps": [{"label": s.label, "kind": s.kind, "reload": s.reload, "gate": s.gate,
                   "device_id": s.device_id} for s in steps],
        "stop_requested": job.stop_requested, "staged_at": job.staged_at,
        "pending_commit": job.pending_commit, "abort_deadline": job.abort_deadline,
        "allowed": jb.allowed_actions(db, job, busy),
    }


def job_detail(db: Session, job: UpgradeJob, settings) -> dict:
    out = job_summary(db, job, settings)
    names = {u["device_id"]: u["name"] for u in out["units"]}
    out["checks"] = {phase: [_check_out(c, names) for c in jb.latest_checks(db, job.id, phase)]
                     for phase in ("pre", "stage", "post")}
    out["events"] = [{"time": e.time, "level": e.level, "step": e.step, "message": e.message,
                      "device": names.get(e.device_id, "")}
                     for e in db.scalars(select(JobEvent).where(JobEvent.job_id == job.id)
                                         .order_by(JobEvent.id))]
    circ = {}
    for stage in ("planned", "started"):
        row = db.scalar(select(JobCircuits).where(JobCircuits.job_id == job.id,
                                                  JobCircuits.stage == stage)
                        .order_by(JobCircuits.id.desc()).limit(1))
        circ[stage] = _circuits_out(row)
    out["circuits"] = circ
    cred = _upgrade_cred(db, job.device_id)
    out["upgrade_credential"] = cred.name if cred else None
    return out


def _job(db: Session, job_id: int) -> UpgradeJob:
    job = db.get(UpgradeJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


# --- jobs -------------------------------------------------------------------

class JobIn(BaseModel):
    device_id: int
    image_id: int
    path: str = Field(default="", max_length=50)
    live: bool = False
    change_ref: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=5000)
    planned_start: datetime | None = None
    planned_end: datetime | None = None


@router.get("/jobs")
def list_jobs(request: Request, device_id: int | None = None, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = select(UpgradeJob).order_by(UpgradeJob.id.desc()).limit(500)
    if device_id is not None:
        member = select(JobUnit.job_id).where(JobUnit.device_id == device_id)
        q = q.where((UpgradeJob.device_id == device_id) | UpgradeJob.id.in_(member))
    return [job_summary(db, j, _settings(request)) for j in db.scalars(q)]


def _open_job_for(db: Session, device_id: int) -> UpgradeJob | None:
    member = select(JobUnit.job_id).where(JobUnit.device_id == device_id)
    return db.scalar(select(UpgradeJob).where(
        (UpgradeJob.device_id == device_id) | UpgradeJob.id.in_(member),
        UpgradeJob.status.not_in(CLOSED_STATES)).limit(1))


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_upgrader)):
    settings = _settings(request)
    device = db.get(Device, body.device_id)
    image = db.get(FirmwareImage, body.image_id)
    if device is None or image is None:
        raise HTTPException(422, "Device or image not found")
    driver = drivers.for_platform(device.platform)
    if driver is None:
        raise HTTPException(422, f"No upgrade driver for {device.platform}")
    if image.platform != device.platform:
        raise HTTPException(422, f"{image.filename} is for {image.platform}, "
                                 f"not {device.platform}")
    facts = db.get(DeviceFacts, device.id)
    if facts and facts.model and not pattern_matches(image.model_pattern, facts.model):
        raise HTTPException(422, f"{image.filename} is for models {image.model_pattern}; "
                                 f"{device.name} is a {facts.model}")
    dev_settings = db.get(DeviceUpgradeSettings, device.id)
    path_id = body.path or drivers.default_path(
        device.platform, {"boot_mode": facts.boot_mode if facts else None},
        dev_settings.default_path if dev_settings else "",
        bool(dev_settings and dev_settings.peer_device_id))
    path = driver.path(path_id)
    if path is None:
        raise HTTPException(422, f"Unknown upgrade path '{path_id}' for {device.platform}")
    units = [device]
    if path.units == 2:
        peer = db.get(Device, dev_settings.peer_device_id) if (
            dev_settings and dev_settings.peer_device_id) else None
        if peer is None:
            raise HTTPException(422, f"'{path.label}' needs the pair's other unit: an admin sets "
                                     f"it as the peer in {device.name}'s upgrade settings")
        if peer.platform != device.platform:
            raise HTTPException(422, f"The peer {peer.name} isn't a {device.platform} device")
        units.append(peer)
    if body.planned_start and body.planned_end and body.planned_end <= body.planned_start:
        raise HTTPException(422, "The planned window ends before it starts")
    if body.live:
        if not settings.live_allowed(device.platform, path.id):
            raise HTTPException(422, "Live upgrades are switched off for this platform "
                                     "(NETOPS_UPGRADE_LIVE_PLATFORMS); create a dry run")
        if not (body.planned_start and body.planned_end):
            raise HTTPException(422, "A live upgrade needs a change window (start and end)")
    for u in units:
        open_job = _open_job_for(db, u.id)
        if open_job:
            raise HTTPException(409, f"{u.name} already has an open upgrade job (#{open_job.id})")
    job = UpgradeJob(device_id=device.id, image_id=image.id, target_version=image.version,
                     path=path.id, from_version=facts.version if facts and facts.version else "",
                     change_ref=body.change_ref.strip(), notes=body.notes,
                     planned_start=body.planned_start, planned_end=body.planned_end,
                     created_by=user.username, dry_run=not body.live)
    db.add(job)
    db.flush()
    for pos, u in enumerate(units):
        f = db.get(DeviceFacts, u.id)
        db.add(JobUnit(job_id=job.id, device_id=u.id, position=pos,
                       from_version=f.version if f and f.version else ""))
    db.flush()
    n = jb.freeze_circuits(db, job, "planned")
    jb.event(db, job.id, f"Job planned by {user.username} ({'LIVE' if body.live else 'DRY RUN'}, "
                         f"{path.label}): {job.from_version or '?'} → {job.target_version} with "
                         f"{image.filename} on {', '.join(u.name for u in units)}; "
                         f"{n} affected circuit(s)", "plan", "action")
    audit(db, user.username, "firmware.job.create",
          f"#{job.id} {', '.join(u.name for u in units)} -> {image.version} ({image.filename}), "
          f"{path.id}, {'live' if body.live else 'dry run'}")
    return job_detail(db, job, settings)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request, db: Session = Depends(get_db),
            _: User = Depends(current_user)):
    return job_detail(db, _job(db, job_id), _settings(request))


class ActionIn(BaseModel):
    confirm: str = Field(default="", max_length=200)  # live Start: the device name, typed


def _action(action: str):
    def handler(job_id: int, request: Request, body: ActionIn | None = None,
                db: Session = Depends(get_db), user: User = Depends(require_upgrader)):
        job = _job(db, job_id)
        if action in ("start", "continue", "rollback") and not job.dry_run:
            name = db.get(Device, job.device_id).name
            if (body.confirm if body else "").strip().lower() != name.lower():
                raise HTTPException(422, f"Type the device name ({name}) to confirm a live "
                                         f"{action}")
        try:
            jb.request_action(db, _settings(request), job, action, user.username)
        except jb.JobError as exc:
            raise HTTPException(409, str(exc)) from None
        audit(db, user.username, f"firmware.job.{action}",
              f"#{job.id}{'' if job.dry_run else ' LIVE'}")
        return {"queued": True}
    handler.__name__ = f"job_{action}"
    return handler


for _a in jb.ACTIONS:
    router.add_api_route(f"/jobs/{{job_id}}/{_a}", _action(_a), methods=["POST"], status_code=202)


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_upgrader)):
    job = _job(db, job_id)
    try:
        jb.request_stop(db, job, user.username)
    except jb.JobError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.username, "firmware.job.stop", f"#{job.id}")
    return job_detail(db, job, _settings(request))


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/jobs/{job_id}/checks/{check_row_id}/override")
def override_check(job_id: int, check_row_id: int, body: ReasonIn, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(require_upgrader)):
    job = _job(db, job_id)
    check = db.get(JobCheck, check_row_id)
    if check is None:
        raise HTTPException(404, "Check not found")
    if jb.pending_request(db, job_id) or job.status in BUSY_STATES:
        raise HTTPException(409, "Wait for the running step to finish")
    try:
        jb.override(db, job, check, user.username, body.reason.strip())
    except jb.JobError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.username, "firmware.job.override",
          f"#{job.id} {check.phase} '{check.label}': {body.reason.strip()}")
    return job_detail(db, job, _settings(request))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, body: ReasonIn, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_upgrader)):
    job = _job(db, job_id)
    try:
        jb.cancel(db, job, user.username, body.reason.strip())
    except jb.JobError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.username, "firmware.job.cancel", f"#{job.id}: {body.reason.strip()}")
    return job_detail(db, job, _settings(request))


@router.get("/jobs/{job_id}/report.html")
def report_html(job_id: int, request: Request, db: Session = Depends(get_db),
                _: User = Depends(current_user)):
    job = job_detail(db, _job(db, job_id), _settings(request))
    return Response(render_html(job), media_type="text/html", headers={
        # The report has its own inline styles and no scripts.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Content-Disposition": f'inline; filename="upgrade-job-{job_id}.html"'})


@router.get("/jobs/{job_id}/report.csv")
def report_csv(job_id: int, request: Request, db: Session = Depends(get_db),
               _: User = Depends(current_user)):
    job = job_detail(db, _job(db, job_id), _settings(request))
    return Response(render_csv(job), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="upgrade-job-{job_id}.csv"'})


# --- drivers and the worker ---------------------------------------------------------

@router.get("/upgrade-paths")
def upgrade_paths(request: Request, _: User = Depends(current_user)):
    s = _settings(request)
    out = drivers.catalogue()
    for d in out:
        for p in d["paths"]:
            p["live_allowed"] = s.live_allowed(d["platform"], p["id"])
    return out


@router.get("/worker")
def get_worker(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return jb.worker_status(db)


# --- per-device upgrade settings -------------------------------------------------

class DeviceSettingsIn(BaseModel):
    upgrade_credential_id: int | None = None
    default_path: str = Field(default="", max_length=50)
    peer_device_id: int | None = None
    file_system: str = Field(default="", max_length=30, pattern=r"^[A-Za-z0-9:/_-]*$")
    fdm_fingerprint: str = Field(default="", max_length=100, pattern=r"^[0-9A-Fa-f:]*$")


@router.get("/devices/{device_id}/settings")
def get_device_settings(device_id: int, db: Session = Depends(get_db),
                        _: User = Depends(current_user)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    s = db.get(DeviceUpgradeSettings, device_id)
    cred = _upgrade_cred(db, device_id)
    peer = db.get(Device, s.peer_device_id) if s and s.peer_device_id else None
    driver = drivers.for_platform(device.platform)
    return {"device_id": device_id, "upgrade_credential_id": s.upgrade_credential_id if s else None,
            "upgrade_credential": cred.name if cred else None,
            "default_path": s.default_path if s else "",
            "peer_device_id": peer.id if peer else None, "peer": peer.name if peer else None,
            "file_system": s.file_system if s else "",
            "fdm_fingerprint": s.fdm_fingerprint if s else "",
            "paths": [vars(p) for p in driver.paths] if driver else []}


@router.put("/devices/{device_id}/settings")
def put_device_settings(device_id: int, body: DeviceSettingsIn, db: Session = Depends(get_db),
                        user: User = Depends(require_admin)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    if body.upgrade_credential_id is not None and db.get(Credential, body.upgrade_credential_id) is None:
        raise HTTPException(422, "Credential does not exist")
    driver = drivers.for_platform(device.platform)
    if body.default_path and not (driver and driver.path(body.default_path)):
        raise HTTPException(422, f"Unknown upgrade path '{body.default_path}'")
    if body.peer_device_id is not None:
        peer = db.get(Device, body.peer_device_id)
        if peer is None or peer.id == device_id:
            raise HTTPException(422, "Pick another existing device as the peer")
        if peer.platform != device.platform:
            raise HTTPException(422, f"{peer.name} isn't a {device.platform} device")
    s = db.get(DeviceUpgradeSettings, device_id) or DeviceUpgradeSettings(device_id=device_id)
    old_peer = s.peer_device_id
    s.upgrade_credential_id, s.default_path = body.upgrade_credential_id, body.default_path
    s.peer_device_id, s.file_system = body.peer_device_id, body.file_system.strip()
    s.fdm_fingerprint = body.fdm_fingerprint.strip().upper()
    db.add(s)
    # A pair is symmetrical: point the peer back at this device too
    if body.peer_device_id and body.peer_device_id != old_peer:
        ps = db.get(DeviceUpgradeSettings, body.peer_device_id) or \
            DeviceUpgradeSettings(device_id=body.peer_device_id)
        ps.peer_device_id = device_id
        ps.default_path = ps.default_path or body.default_path
        db.add(ps)
    cred = db.get(Credential, body.upgrade_credential_id) if body.upgrade_credential_id else None
    audit(db, user.username, "firmware.device.settings",
          f"{device.name}: upgrade account {cred.name if cred else 'none'}, path "
          f"{body.default_path or 'default'}, peer {body.peer_device_id or 'none'}"
          + (f", FDM certificate {s.fdm_fingerprint}" if s.fdm_fingerprint else ""))
    return get_device_settings(device_id, db, user)


@router.post("/devices/{device_id}/fdm-fingerprint")
def fetch_fdm_fingerprint(device_id: int, request: Request, db: Session = Depends(get_db),
                          _: User = Depends(require_admin)):
    """Read the certificate FDM presents now, for an admin to compare and trust."""
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    try:
        return {"fingerprint": request.app.state.upgrade_io.fetch_fingerprint(device.address)}
    except DeviceError as exc:
        raise HTTPException(502, str(exc)) from None


@router.get("/devices/{device_id}/jobs")
def device_history(device_id: int, request: Request, db: Session = Depends(get_db),
                   _: User = Depends(current_user)):
    return list_jobs(request, device_id, db, _)


# --- repository details ------------------------------------------------------------

class ImageInfoIn(BaseModel):
    recommended: bool = False
    release_ref: str = Field(default="", max_length=500)


@router.put("/images/{image_id}/info")
def put_image_info(image_id: int, body: ImageInfoIn, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    image = db.get(FirmwareImage, image_id)
    if image is None:
        raise HTTPException(404, "Image not found")
    info = db.get(ImageInfo, image_id) or ImageInfo(image_id=image_id)
    info.recommended, info.release_ref = body.recommended, body.release_ref.strip()
    db.add(info)
    audit(db, user.username, "firmware.image.info",
          f"{image.filename}: recommended={body.recommended}")
    return {"image_id": image_id, "recommended": info.recommended, "release_ref": info.release_ref}
