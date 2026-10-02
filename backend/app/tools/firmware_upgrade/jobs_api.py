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
from app.tools.firmware_upgrade import jobs as jb
from app.tools.firmware_upgrade.facts import pattern_matches, vendor_for
from app.tools.firmware_upgrade.models import (CLOSED_STATES, DeviceFacts, DeviceUpgradeSettings,
                                               FirmwareImage, ImageInfo, JobCheck, JobCircuits,
                                               JobEvent, UpgradeJob)
from app.tools.firmware_upgrade.report import render_csv, render_html

router = APIRouter(prefix="/api/firmware", tags=["upgrade jobs"])


def _svc(request: Request) -> jb.JobService:
    return request.app.state.job_service


def _check_out(c: JobCheck) -> dict:
    return {"id": c.id, "run_no": c.run_no, "check_id": c.check_id, "label": c.label,
            "severity": c.severity, "status": c.status, "value": c.value, "detail": c.detail,
            "overridden_by": c.overridden_by, "override_reason": c.override_reason,
            "overridden_at": c.overridden_at}


def _circuits_out(row: JobCircuits | None) -> dict | None:
    if row is None:
        return None
    return {"count": row.count, "source": row.source, "created_at": row.created_at,
            "groups": json.loads(row.data)}


def _upgrade_cred(db: Session, device_id: int) -> Credential | None:
    s = db.get(DeviceUpgradeSettings, device_id)
    return db.get(Credential, s.upgrade_credential_id) if s and s.upgrade_credential_id else None


def job_summary(db: Session, job: UpgradeJob, svc: jb.JobService) -> dict:
    d = db.get(Device, job.device_id)
    img = db.get(FirmwareImage, job.image_id)
    busy = svc.is_busy(job.id)
    return {
        "id": job.id, "status": job.status, "busy": busy, "dry_run": job.dry_run,
        "device": {"id": d.id, "name": d.name, "address": d.address, "platform": d.platform,
                   "platform_label": PLATFORMS[d.platform].label if d.platform in PLATFORMS
                   else d.platform, "site": d.site},
        "image": {"id": img.id, "filename": img.filename, "version": img.version, "md5": img.md5,
                  "size": img.size, "vendor": vendor_for(img.platform)},
        "from_version": job.from_version, "target_version": job.target_version,
        "change_ref": job.change_ref, "notes": job.notes,
        "planned_start": job.planned_start, "planned_end": job.planned_end,
        "created_by": job.created_by, "created_at": job.created_at,
        "started_by": job.started_by, "started_at": job.started_at,
        "finished_at": job.finished_at, "outcome_note": job.outcome_note,
        "pre_backup_commit": job.pre_backup_commit, "post_backup_commit": job.post_backup_commit,
        "pre_run": job.pre_run, "post_run": job.post_run,
        "allowed": [] if busy else sorted(a for a, states in jb.ALLOWED.items()
                                          if job.status in states),
    }


def job_detail(db: Session, job: UpgradeJob, svc: jb.JobService) -> dict:
    out = job_summary(db, job, svc)
    out["checks"] = {"pre": [_check_out(c) for c in jb._latest(db, job.id, "pre", job.pre_run)],
                     "post": [_check_out(c) for c in jb._latest(db, job.id, "post", job.post_run)]}
    out["events"] = [{"time": e.time, "level": e.level, "step": e.step, "message": e.message}
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
    change_ref: str = Field(default="", max_length=100)
    notes: str = Field(default="", max_length=5000)
    planned_start: datetime | None = None
    planned_end: datetime | None = None


@router.get("/jobs")
def list_jobs(request: Request, device_id: int | None = None, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    q = select(UpgradeJob).order_by(UpgradeJob.id.desc()).limit(500)
    if device_id is not None:
        q = q.where(UpgradeJob.device_id == device_id)
    return [job_summary(db, j, _svc(request)) for j in db.scalars(q)]


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_upgrader)):
    device = db.get(Device, body.device_id)
    image = db.get(FirmwareImage, body.image_id)
    if device is None or image is None:
        raise HTTPException(422, "Device or image not found")
    if image.platform != device.platform:
        raise HTTPException(422, f"{image.filename} is for {image.platform}, "
                                 f"not {device.platform}")
    facts = db.get(DeviceFacts, device.id)
    if facts and facts.model and not pattern_matches(image.model_pattern, facts.model):
        raise HTTPException(422, f"{image.filename} is for models {image.model_pattern}; "
                                 f"{device.name} is a {facts.model}")
    if body.planned_start and body.planned_end and body.planned_end <= body.planned_start:
        raise HTTPException(422, "The planned window ends before it starts")
    open_job = db.scalar(select(UpgradeJob).where(UpgradeJob.device_id == device.id,
                                                  UpgradeJob.status.not_in(CLOSED_STATES)))
    if open_job:
        raise HTTPException(409, f"{device.name} already has an open upgrade job (#{open_job.id})")
    job = UpgradeJob(device_id=device.id, image_id=image.id, target_version=image.version,
                     from_version=facts.version if facts and facts.version else "",
                     change_ref=body.change_ref.strip(), notes=body.notes,
                     planned_start=body.planned_start, planned_end=body.planned_end,
                     created_by=user.username, dry_run=True)
    db.add(job)
    db.flush()
    n = jb.freeze_circuits(db, job, "planned")
    jb.event(db, job.id, f"Job planned by {user.username}: {job.from_version or '?'} → "
                         f"{job.target_version} with {image.filename}; {n} affected circuit(s)",
             "plan", "action")
    audit(db, user.username, "firmware.job.create",
          f"#{job.id} {device.name} -> {image.version} ({image.filename})")
    return job_detail(db, job, _svc(request))


@router.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request, db: Session = Depends(get_db),
            _: User = Depends(current_user)):
    return job_detail(db, _job(db, job_id), _svc(request))


def _action(action: str):
    def handler(job_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_upgrader)):
        job = _job(db, job_id)
        try:
            _svc(request).submit(job_id, action, user.username)
        except jb.JobError as exc:
            raise HTTPException(409, str(exc)) from None
        audit(db, user.username, f"firmware.job.{action}", f"#{job.id}")
        return {"queued": True}
    handler.__name__ = f"job_{action}"
    return handler


for _a in ("precheck", "start", "postcheck", "rollback"):
    router.add_api_route(f"/jobs/{{job_id}}/{_a}", _action(_a), methods=["POST"], status_code=202)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/jobs/{job_id}/checks/{check_row_id}/override")
def override_check(job_id: int, check_row_id: int, body: ReasonIn, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(require_upgrader)):
    job = _job(db, job_id)
    check = db.get(JobCheck, check_row_id)
    if check is None:
        raise HTTPException(404, "Check not found")
    if _svc(request).is_busy(job_id):
        raise HTTPException(409, "Wait for the running step to finish")
    try:
        jb.override(db, job, check, user.username, body.reason.strip())
    except jb.JobError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.username, "firmware.job.override",
          f"#{job.id} {check.phase} '{check.label}': {body.reason.strip()}")
    return job_detail(db, job, _svc(request))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, body: ReasonIn, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_upgrader)):
    job = _job(db, job_id)
    if _svc(request).is_busy(job_id):
        raise HTTPException(409, "Wait for the running step to finish")
    try:
        jb.cancel(db, job, user.username, body.reason.strip())
    except jb.JobError as exc:
        raise HTTPException(409, str(exc)) from None
    audit(db, user.username, "firmware.job.cancel", f"#{job.id}: {body.reason.strip()}")
    return job_detail(db, job, _svc(request))


@router.get("/jobs/{job_id}/report.html")
def report_html(job_id: int, request: Request, db: Session = Depends(get_db),
                _: User = Depends(current_user)):
    job = job_detail(db, _job(db, job_id), _svc(request))
    return Response(render_html(job), media_type="text/html", headers={
        # The report has its own inline styles and no scripts.
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "Content-Disposition": f'inline; filename="upgrade-job-{job_id}.html"'})


@router.get("/jobs/{job_id}/report.csv")
def report_csv(job_id: int, request: Request, db: Session = Depends(get_db),
               _: User = Depends(current_user)):
    job = job_detail(db, _job(db, job_id), _svc(request))
    return Response(render_csv(job), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="upgrade-job-{job_id}.csv"'})


# --- per-device upgrade settings -------------------------------------------------

class DeviceSettingsIn(BaseModel):
    upgrade_credential_id: int | None = None
    peer_group: str = Field(default="", max_length=100)


@router.get("/devices/{device_id}/settings")
def get_device_settings(device_id: int, db: Session = Depends(get_db),
                        _: User = Depends(current_user)):
    s = db.get(DeviceUpgradeSettings, device_id)
    cred = _upgrade_cred(db, device_id)
    return {"device_id": device_id, "upgrade_credential_id": s.upgrade_credential_id if s else None,
            "upgrade_credential": cred.name if cred else None,
            "peer_group": s.peer_group if s else ""}


@router.put("/devices/{device_id}/settings")
def put_device_settings(device_id: int, body: DeviceSettingsIn, db: Session = Depends(get_db),
                        user: User = Depends(require_admin)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    if body.upgrade_credential_id is not None and db.get(Credential, body.upgrade_credential_id) is None:
        raise HTTPException(422, "Credential does not exist")
    s = db.get(DeviceUpgradeSettings, device_id) or DeviceUpgradeSettings(device_id=device_id)
    s.upgrade_credential_id, s.peer_group = body.upgrade_credential_id, body.peer_group.strip()
    db.add(s)
    cred = db.get(Credential, body.upgrade_credential_id) if body.upgrade_credential_id else None
    audit(db, user.username, "firmware.device.settings",
          f"{device.name}: upgrade credential {cred.name if cred else 'none'}")
    return get_device_settings(device_id, db, user)


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
