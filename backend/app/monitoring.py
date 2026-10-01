"""Health figures for PRTG (an "HTTP Data Advanced" sensor), in PRTG's JSON format.

    GET /api/monitoring/prtg     header:  Authorization: Bearer <NETOPS_MONITORING_TOKEN>

Disabled (404) until NETOPS_MONITORING_TOKEN is set. The limits below are applied
by PRTG when it first creates each channel; change them in PRTG afterwards.
"""

import hmac
import shutil
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db, utcnow
from app.core.models import Device
from app.tools.config_backup.models import FAILED, NEVER, BackupState
from app.tools.config_backup.service import ensure_states
from app.tools.firmware_upgrade.api import _rows as firmware_rows
from app.tools.firmware_upgrade.facts import BEHIND

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"], include_in_schema=False)

LAST_BACKUP_FILE = "last-backup"  # written into the data folder by netops-backup


def _check_token(request: Request) -> None:
    expected = request.app.state.settings.monitoring_token
    if not expected:
        raise HTTPException(404, "Not Found")
    auth = request.headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Invalid monitoring token")


def _channel(name: str, value, unit: str = "Count", **limits) -> dict:
    ch = {"channel": name, "value": value, "unit": unit}
    if isinstance(value, float):
        ch["float"] = 1
    if limits:
        ch["limitmode"] = 1
        ch.update({f"limit{k.replace('_', '')}": v for k, v in limits.items()})
    return ch


@router.get("/prtg")
def prtg(request: Request, db: Session = Depends(get_db)):
    _check_token(request)
    settings = request.app.state.settings
    now = utcnow()

    ensure_states(db, settings.default_frequency_minutes)
    failing = never = stale = 0
    for state, enabled in db.execute(select(BackupState, Device.enabled)
                                     .join(Device, Device.id == BackupState.device_id)):
        if not (enabled and state.enabled):
            continue
        failing += state.last_status == FAILED
        never += state.last_status == NEVER
        stale += state.last_success is None or state.last_success < now - timedelta(hours=48)

    fw = firmware_rows(db, request.app.state.firmware_service)
    behind = sum(r.compliance == BEHIND for r in fw)
    fw_failed = sum(r.facts.status == "failed" for r in fw)

    disk = shutil.disk_usage(settings.data_dir)
    free_pct = round(100 * disk.free / disk.total, 1)

    marker = settings.data_dir / LAST_BACKUP_FILE
    if marker.exists():
        mtime = datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc).replace(tzinfo=None)
        backup_age = round((now - mtime).total_seconds() / 3600, 1)
    else:
        backup_age = 9999.0

    result = [
        _channel("Devices failing backup", failing, max_warning=0, max_error=5),
        _channel("Devices without a good backup in 48 h", stale, max_error=0),
        _channel("Devices never backed up", never),
        _channel("Firmware: devices behind standard", behind),
        _channel("Firmware: version checks failing", fw_failed, max_warning=0),
        _channel("Data disk free", free_pct, "Percent", min_warning=20, min_error=10),
        _channel("Hours since server backup", backup_age, "Custom", max_warning=26, max_error=50),
    ]
    result[-1]["customunit"] = "h"
    text = (f"{failing} device(s) failing backup, {behind} behind firmware standard"
            if failing or behind else "OK")
    return {"prtg": {"result": result, "text": text}}
