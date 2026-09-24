"""HTTP API for the config backup tool."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import User, current_user, require_admin
from app.core.db import get_db
from app.core.inventory import DeviceOut, device_out
from app.core.models import Device
from app.tools.config_backup.models import BackupRun, BackupState
from app.tools.config_backup.service import BackupService, ensure_states

router = APIRouter(prefix="/api/backup", tags=["config-backup"])


def _service(request: Request) -> BackupService:
    return request.app.state.backup_service


class BackupStatus(BaseModel):
    frequency_minutes: int
    enabled: bool
    status: str
    busy: bool
    last_attempt: datetime | None
    last_success: datetime | None
    last_change: datetime | None
    last_error_type: str | None
    last_error: str | None
    consecutive_failures: int


class DeviceBackupRow(BaseModel):
    device: DeviceOut
    backup: BackupStatus


class SettingsIn(BaseModel):
    frequency_minutes: int = Field(ge=15, le=43200)  # 15 min .. 30 days
    enabled: bool = True


def _row(device: Device, state: BackupState, svc: BackupService) -> DeviceBackupRow:
    return DeviceBackupRow(device=device_out(device), backup=BackupStatus(
        frequency_minutes=state.frequency_minutes, enabled=state.enabled,
        status=state.last_status, busy=svc.is_busy(device.id),
        last_attempt=state.last_attempt, last_success=state.last_success,
        last_change=state.last_change, last_error_type=state.last_error_type,
        last_error=state.last_error, consecutive_failures=state.consecutive_failures,
    ))


def _load(db: Session, device_id: int, svc: BackupService) -> tuple[Device, BackupState]:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    ensure_states(db, svc.settings.default_frequency_minutes)
    return device, db.get(BackupState, device_id)


@router.get("/devices", response_model=list[DeviceBackupRow])
def list_backup_devices(request: Request, db: Session = Depends(get_db),
                        _: User = Depends(current_user)):
    svc = _service(request)
    ensure_states(db, svc.settings.default_frequency_minutes)
    rows = db.execute(select(Device, BackupState)
                      .join(BackupState, BackupState.device_id == Device.id)
                      .order_by(Device.site, Device.name)).all()
    return [_row(d, s, svc) for d, s in rows]


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db), _: User = Depends(current_user)):
    svc = _service(request)
    ensure_states(db, svc.settings.default_frequency_minutes)
    counts = {"total": 0, "success": 0, "failed": 0, "never": 0, "running": 0, "disabled": 0}
    for state, enabled in db.execute(select(BackupState, Device.enabled)
                                     .join(Device, Device.id == BackupState.device_id)):
        counts["total"] += 1
        if not (enabled and state.enabled):
            counts["disabled"] += 1
        else:
            counts[state.last_status] = counts.get(state.last_status, 0) + 1
    return counts


@router.get("/devices/{device_id}", response_model=DeviceBackupRow)
def get_backup_device(device_id: int, request: Request, db: Session = Depends(get_db),
                      _: User = Depends(current_user)):
    svc = _service(request)
    return _row(*_load(db, device_id, svc), svc)


@router.put("/devices/{device_id}/settings", response_model=DeviceBackupRow)
def update_settings(device_id: int, body: SettingsIn, request: Request,
                    db: Session = Depends(get_db), user: User = Depends(require_admin)):
    svc = _service(request)
    device, state = _load(db, device_id, svc)
    state.frequency_minutes, state.enabled = body.frequency_minutes, body.enabled
    audit(db, user.username, "backup.settings",
          f"{device.name}: every {body.frequency_minutes} min, enabled={body.enabled}")
    return _row(device, state, svc)


@router.post("/devices/{device_id}/run", status_code=202)
def run_now(device_id: int, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_admin)):
    svc = _service(request)
    device, _state = _load(db, device_id, svc)
    queued = svc.submit(device_id, f"manual:{user.username}")
    if queued:
        audit(db, user.username, "backup.run", device.name)
    return {"queued": queued, "message": "Backup queued" if queued else "Backup already running"}


@router.get("/devices/{device_id}/runs")
def list_runs(device_id: int, limit: int = 50, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    runs = db.scalars(select(BackupRun).where(BackupRun.device_id == device_id)
                      .order_by(BackupRun.id.desc()).limit(min(limit, 500)))
    return [{"id": r.id, "trigger": r.trigger, "started_at": r.started_at,
             "finished_at": r.finished_at, "status": r.status, "changed": r.changed,
             "commit": r.commit, "error_type": r.error_type, "message": r.message}
            for r in runs]


# --- Stored config versions (contain secrets: admin-only by default) --------

def config_reader(request: Request, user: User = Depends(current_user)) -> User:
    if not (user.is_admin or request.app.state.settings.viewers_can_read_configs):
        raise HTTPException(403, "Viewing configurations requires the administrator role")
    return user


def _config_path(db: Session, device_id: int) -> str:
    state = db.get(BackupState, device_id)
    if state is None or not state.config_path:
        raise HTTPException(404, "No configuration has been backed up for this device yet")
    return state.config_path


@router.get("/devices/{device_id}/versions")
def list_versions(device_id: int, request: Request, db: Session = Depends(get_db),
                  _: User = Depends(config_reader)):
    return _service(request).store.history(_config_path(db, device_id))


def _resolve(versions: list[dict], commit: str) -> dict:
    for v in versions:
        if v["commit"].startswith(commit):
            return v
    raise HTTPException(404, "Version not found for this device")


@router.get("/devices/{device_id}/config", response_class=PlainTextResponse)
def get_config(device_id: int, request: Request, version: str | None = None,
               db: Session = Depends(get_db), user: User = Depends(config_reader)):
    store = _service(request).store
    path = _config_path(db, device_id)
    versions = store.history(path)
    if not versions:
        raise HTTPException(404, "No stored versions")
    v = _resolve(versions, version) if version else versions[0]
    audit(db, user.username, "backup.view_config", f"device {device_id} @ {v['commit'][:8]}")
    return store.read(v["path"], v["commit"])


@router.get("/devices/{device_id}/diff", response_class=PlainTextResponse)
def get_diff(device_id: int, request: Request, to: str | None = None,
             from_: str | None = Query(None, alias="from"), db: Session = Depends(get_db),
             _: User = Depends(config_reader)):
    """Diff two versions. Defaults: `to` = newest, `from` = the version before `to`."""
    store = _service(request).store
    versions = store.history(_config_path(db, device_id))
    if not versions:
        raise HTTPException(404, "No stored versions")
    new = _resolve(versions, to) if to else versions[0]
    if from_:
        old = _resolve(versions, from_)
    else:
        idx = versions.index(new)
        if idx + 1 >= len(versions):
            return ""
        old = versions[idx + 1]
    return store.diff((old["path"], old["commit"]), (new["path"], new["commit"]))
