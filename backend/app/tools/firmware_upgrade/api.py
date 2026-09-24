"""HTTP API for the firmware tool: version report, standards and image library."""

import csv
import io
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import User, current_user, require_admin
from app.core.db import get_db, session_scope
from app.core.inventory import DeviceOut, device_out
from app.core.models import Device
from app.core.platforms import PLATFORMS
from app.tools.firmware_upgrade.facts import (AHEAD, BEHIND, COMPLIANT, NO_STANDARD, UNKNOWN,
                                              best_standard, compliance)
from app.tools.firmware_upgrade.images import (ImageError, ImageWriter, check_expected_hash,
                                               delete_image, valid_filename)
from app.tools.firmware_upgrade.models import DeviceFacts, FirmwareImage, FirmwareStandard
from app.tools.firmware_upgrade.service import FirmwareService, ensure_facts

router = APIRouter(prefix="/api/firmware", tags=["firmware"])

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()-]{0,99}$")
_PATTERN_RE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9*?._()/-]{0,99}$")


def _service(request: Request) -> FirmwareService:
    return request.app.state.firmware_service


def _check_platform(value: str) -> str:
    if value not in PLATFORMS:
        raise ValueError(f"unknown platform; choose one of {', '.join(PLATFORMS)}")
    return value


def _check_version(value: str) -> str:
    value = value.strip()
    if not _VERSION_RE.match(value):
        raise ValueError("use letters, digits and . _ - ( ) (e.g. 17.09.04a or 9.18(4))")
    return value


def _check_pattern(value: str) -> str:
    value = value.strip() or "*"
    if not _PATTERN_RE.match(value):
        raise ValueError("use a model name, optionally with * wildcards (e.g. C9300-*)")
    return value


# --- Version report ---------------------------------------------------------

class FactsOut(BaseModel):
    status: str
    busy: bool
    last_attempt: datetime | None
    last_success: datetime | None
    last_error_type: str | None
    last_error: str | None
    model: str | None
    serial: str | None
    version: str | None
    image: str | None
    boot_mode: str | None
    ha_role: str | None
    flash_total: int | None
    flash_free: int | None
    previous_version: str | None
    version_changed_at: datetime | None


class DeviceFirmwareRow(BaseModel):
    device: DeviceOut
    facts: FactsOut
    target_version: str | None
    standard_id: int | None
    target_image: str | None
    compliance: str


def _rows(db: Session, svc: FirmwareService, device_id: int | None = None):
    ensure_facts(db)
    standards = db.scalars(select(FirmwareStandard)).all()
    images = {i.id: i.filename for i in db.scalars(select(FirmwareImage))}
    query = (select(Device, DeviceFacts).join(DeviceFacts, DeviceFacts.device_id == Device.id)
             .order_by(Device.site, Device.name))
    if device_id is not None:
        query = query.where(Device.id == device_id)
    rows = []
    for device, f in db.execute(query).all():
        std = best_standard(standards, device.platform, f.model)
        rows.append(DeviceFirmwareRow(
            device=device_out(device),
            facts=FactsOut(
                status=f.last_status, busy=svc.is_busy(device.id), last_attempt=f.last_attempt,
                last_success=f.last_success, last_error_type=f.last_error_type,
                last_error=f.last_error, model=f.model, serial=f.serial, version=f.version,
                image=f.image, boot_mode=f.boot_mode, ha_role=f.ha_role,
                flash_total=f.flash_total, flash_free=f.flash_free,
                previous_version=f.previous_version, version_changed_at=f.version_changed_at),
            target_version=std.target_version if std else None,
            standard_id=std.id if std else None,
            target_image=images.get(std.image_id) if std and std.image_id else None,
            compliance=compliance(f.version, std.target_version if std else None),
        ))
    return rows


@router.get("/devices", response_model=list[DeviceFirmwareRow])
def list_devices(request: Request, db: Session = Depends(get_db),
                 _: User = Depends(current_user)):
    return _rows(db, _service(request))


@router.get("/devices.csv")
def export_csv(request: Request, db: Session = Depends(get_db), _: User = Depends(current_user)):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["device", "address", "site", "platform", "model", "serial", "version",
                "target_version", "compliance", "boot_mode", "ha_role", "flash_free_mb",
                "last_checked"])
    for r in _rows(db, _service(request)):
        f = r.facts
        w.writerow([r.device.name, r.device.address, r.device.site, r.device.platform_label,
                    f.model or "", f.serial or "", f.version or "", r.target_version or "",
                    r.compliance, f.boot_mode or "", f.ha_role or "",
                    f.flash_free // 2**20 if f.flash_free is not None else "",
                    f.last_success.isoformat(timespec="minutes") if f.last_success else ""])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="firmware-versions.csv"'})


@router.get("/summary")
def summary(request: Request, db: Session = Depends(get_db), _: User = Depends(current_user)):
    counts = {"total": 0, COMPLIANT: 0, BEHIND: 0, AHEAD: 0, NO_STANDARD: 0, UNKNOWN: 0,
              "check_failed": 0}
    for r in _rows(db, _service(request)):
        counts["total"] += 1
        counts[r.compliance] += 1
        if r.facts.status == "failed":
            counts["check_failed"] += 1
    return counts


@router.get("/devices/{device_id}", response_model=DeviceFirmwareRow)
def get_device(device_id: int, request: Request, db: Session = Depends(get_db),
               _: User = Depends(current_user)):
    rows = _rows(db, _service(request), device_id)
    if not rows:
        raise HTTPException(404, "Device not found")
    return rows[0]


@router.post("/devices/{device_id}/check", status_code=202)
def check_now(device_id: int, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_admin)):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    queued = _service(request).submit(device_id)
    if queued:
        audit(db, user.username, "firmware.check", device.name)
    return {"queued": queued,
            "message": "Version check queued" if queued else "Version check already running"}


@router.post("/check-all", status_code=202)
def check_all(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_admin)):
    svc = _service(request)
    ids = db.scalars(select(Device.id).where(Device.enabled.is_(True))).all()
    queued = sum(svc.submit(i) for i in ids)
    audit(db, user.username, "firmware.check_all", f"{queued} device(s)")
    return {"queued": queued, "message": f"{queued} version check(s) queued"}


# --- Standards (approved versions) -----------------------------------------

class StandardIn(BaseModel):
    platform: str
    model_pattern: str = "*"
    target_version: str
    image_id: int | None = None
    notes: str = Field(default="", max_length=2000)

    @field_validator("platform")
    @classmethod
    def _platform(cls, v: str) -> str:
        return _check_platform(v)

    @field_validator("model_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        return _check_pattern(v)

    @field_validator("target_version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _check_version(v)


def _standard_out(s: FirmwareStandard, images: dict[int, str]) -> dict:
    return {"id": s.id, "platform": s.platform,
            "platform_label": PLATFORMS[s.platform].label if s.platform in PLATFORMS else s.platform,
            "model_pattern": s.model_pattern, "target_version": s.target_version,
            "image_id": s.image_id, "image_filename": images.get(s.image_id),
            "notes": s.notes, "updated_by": s.updated_by, "updated_at": s.updated_at}


def _image_names(db: Session) -> dict[int, str]:
    return {i.id: i.filename for i in db.scalars(select(FirmwareImage))}


@router.get("/standards")
def list_standards(db: Session = Depends(get_db), _: User = Depends(current_user)):
    images = _image_names(db)
    rows = db.scalars(select(FirmwareStandard)
                      .order_by(FirmwareStandard.platform, FirmwareStandard.model_pattern))
    return [_standard_out(s, images) for s in rows]


def _apply_standard(db: Session, std: FirmwareStandard, body: StandardIn, user: User) -> None:
    if body.image_id is not None:
        image = db.get(FirmwareImage, body.image_id)
        if image is None:
            raise HTTPException(422, "Image does not exist")
        if image.platform != body.platform:
            raise HTTPException(422, "Image is for a different platform")
        if image.version != body.target_version:
            raise HTTPException(422, f"Image is version {image.version}, "
                                     f"not {body.target_version}")
    for key, value in body.model_dump().items():
        setattr(std, key, value)
    std.updated_by = user.username
    db.add(std)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A standard for that platform and model pattern already exists") \
            from None


@router.post("/standards", status_code=201)
def create_standard(body: StandardIn, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    std = FirmwareStandard()
    _apply_standard(db, std, body, user)
    audit(db, user.username, "firmware.standard.create",
          f"{std.platform} {std.model_pattern} -> {std.target_version}")
    return _standard_out(std, _image_names(db))


@router.put("/standards/{std_id}")
def update_standard(std_id: int, body: StandardIn, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    std = db.get(FirmwareStandard, std_id)
    if std is None:
        raise HTTPException(404, "Standard not found")
    _apply_standard(db, std, body, user)
    audit(db, user.username, "firmware.standard.update",
          f"{std.platform} {std.model_pattern} -> {std.target_version}")
    return _standard_out(std, _image_names(db))


@router.delete("/standards/{std_id}", status_code=204)
def delete_standard(std_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    std = db.get(FirmwareStandard, std_id)
    if std is None:
        raise HTTPException(404, "Standard not found")
    db.delete(std)
    audit(db, user.username, "firmware.standard.delete", f"{std.platform} {std.model_pattern}")
    return Response(status_code=204)


# --- Image library ----------------------------------------------------------

def _image_out(i: FirmwareImage) -> dict:
    return {"id": i.id, "filename": i.filename, "platform": i.platform,
            "platform_label": PLATFORMS[i.platform].label if i.platform in PLATFORMS else i.platform,
            "version": i.version, "model_pattern": i.model_pattern, "size": i.size,
            "md5": i.md5, "sha512": i.sha512, "verified": i.verified, "notes": i.notes,
            "uploaded_by": i.uploaded_by, "uploaded_at": i.uploaded_at}


@router.get("/images")
def list_images(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(FirmwareImage).order_by(FirmwareImage.platform,
                                                     FirmwareImage.uploaded_at.desc()))
    return [_image_out(i) for i in rows]


@router.put("/images/upload", status_code=201)
async def upload_image(
    request: Request,
    filename: str = Query(...), platform: str = Query(...), version: str = Query(...),
    model_pattern: str = Query("*"), checksum: str = Query(""),
    notes: str = Query("", max_length=2000),
    user: User = Depends(require_admin),
):
    """Upload an image as the raw request body (not multipart), so multi-GB files
    are streamed straight to disk instead of being buffered first."""
    settings = request.app.state.settings
    try:
        filename = valid_filename(filename)
        platform, version = _check_platform(platform), _check_version(version)
        model_pattern, checksum = _check_pattern(model_pattern), check_expected_hash(checksum)
    except (ValueError, ImageError) as exc:
        raise HTTPException(422, str(exc)) from None

    def _exists() -> bool:
        with session_scope() as db:
            return db.scalar(select(FirmwareImage.id)
                             .where(FirmwareImage.filename == filename)) is not None

    if await run_in_threadpool(_exists):
        raise HTTPException(409, "An image with that file name already exists")
    try:
        writer = ImageWriter(settings.firmware_dir, filename,
                             settings.firmware_max_upload_mb * 2**20)
    except ImageError as exc:
        raise HTTPException(409, str(exc)) from None

    buffer = bytearray()
    try:
        async for chunk in request.stream():
            buffer += chunk
            if len(buffer) >= 2**20:
                await run_in_threadpool(writer.write, bytes(buffer))
                buffer.clear()
        if buffer:
            await run_in_threadpool(writer.write, bytes(buffer))
        stored = await run_in_threadpool(writer.finish, checksum)
    except ImageError as exc:
        writer.abort()
        raise HTTPException(422, str(exc)) from None
    except BaseException:  # client went away mid-upload, etc.
        writer.abort()
        raise

    def _record() -> dict:
        with session_scope() as db:
            image = FirmwareImage(filename=filename, platform=platform, version=version,
                                  model_pattern=model_pattern, size=stored.size, md5=stored.md5,
                                  sha512=stored.sha512, verified=bool(checksum), notes=notes,
                                  uploaded_by=user.username)
            db.add(image)
            db.flush()
            audit(db, user.username, "firmware.image.upload",
                  f"{filename} ({version}, {stored.size // 2**20} MB, md5 {stored.md5})")
            return _image_out(image)

    try:
        return await run_in_threadpool(_record)
    except IntegrityError:
        delete_image(settings.firmware_dir, filename)
        raise HTTPException(409, "An image with that file name already exists") from None


@router.delete("/images/{image_id}", status_code=204)
def delete_image_route(image_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require_admin)):
    image = db.get(FirmwareImage, image_id)
    if image is None:
        raise HTTPException(404, "Image not found")
    used = db.scalars(select(FirmwareStandard).where(FirmwareStandard.image_id == image_id)).all()
    if used:
        raise HTTPException(409, f"Image is linked to {len(used)} standard(s) - "
                                 "change those first")
    db.delete(image)
    db.flush()
    delete_image(request.app.state.settings.firmware_dir, image.filename)
    audit(db, user.username, "firmware.image.delete", image.filename)
    return Response(status_code=204)
