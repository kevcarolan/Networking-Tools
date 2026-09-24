"""Device inventory and credential profile API, shared by every tool."""

import ipaddress
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import User, current_user, require_admin
from app.core.db import get_db
from app.core.models import AuditEvent, Credential, Device
from app.core.platforms import PLATFORMS

router = APIRouter(prefix="/api", tags=["inventory"])

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9-]{1,63})*$")


def validate_address(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOST_RE.match(value):
        raise ValueError("must be an IP address or hostname")
    return value


# --- Platforms --------------------------------------------------------------

@router.get("/platforms")
def list_platforms(_: User = Depends(current_user)):
    return [{"key": p.key, "label": p.label} for p in PLATFORMS.values()]


# --- Credentials ------------------------------------------------------------

class CredentialIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=200)
    password: str | None = None  # None on update = keep existing
    enable_secret: str | None = None


class CredentialOut(BaseModel):
    id: int
    name: str
    username: str
    has_enable_secret: bool
    device_count: int
    updated_at: datetime


def _credential_out(c: Credential) -> CredentialOut:
    return CredentialOut(id=c.id, name=c.name, username=c.username,
                         has_enable_secret=bool(c.enable_secret_enc),
                         device_count=len(c.devices), updated_at=c.updated_at)


@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return [_credential_out(c) for c in db.scalars(select(Credential).order_by(Credential.name))]


@router.post("/credentials", response_model=CredentialOut, status_code=201)
def create_credential(body: CredentialIn, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    if not body.password:
        raise HTTPException(422, "Password is required")
    cipher = request.app.state.cipher
    cred = Credential(name=body.name.strip(), username=body.username,
                      password_enc=cipher.encrypt(body.password),
                      enable_secret_enc=cipher.encrypt(body.enable_secret)
                      if body.enable_secret else None)
    db.add(cred)
    _flush_unique(db, "A credential with that name already exists")
    audit(db, user.username, "credential.create", cred.name)
    return _credential_out(cred)


@router.put("/credentials/{cred_id}", response_model=CredentialOut)
def update_credential(cred_id: int, body: CredentialIn, request: Request,
                      db: Session = Depends(get_db), user: User = Depends(require_admin)):
    cred = db.get(Credential, cred_id) or _not_found("Credential")
    cipher = request.app.state.cipher
    cred.name, cred.username = body.name.strip(), body.username
    if body.password:
        cred.password_enc = cipher.encrypt(body.password)
    if body.enable_secret is not None:  # "" clears it
        cred.enable_secret_enc = cipher.encrypt(body.enable_secret) if body.enable_secret else None
    _flush_unique(db, "A credential with that name already exists")
    audit(db, user.username, "credential.update", cred.name)
    return _credential_out(cred)


@router.delete("/credentials/{cred_id}", status_code=204)
def delete_credential(cred_id: int, db: Session = Depends(get_db),
                      user: User = Depends(require_admin)):
    cred = db.get(Credential, cred_id) or _not_found("Credential")
    if cred.devices:
        raise HTTPException(409, f"Credential is used by {len(cred.devices)} device(s)")
    db.delete(cred)
    audit(db, user.username, "credential.delete", cred.name)
    return Response(status_code=204)


# --- Devices ----------------------------------------------------------------

class DeviceIn(BaseModel):
    name: str
    address: str
    platform: str
    site: str = Field(default="", max_length=100)
    credential_id: int | None = None
    enabled: bool = True
    notes: str = Field(default="", max_length=5000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not _NAME_RE.match(v):
            raise ValueError("use letters, digits, '.', '_' or '-' (max 100)")
        return v

    @field_validator("address")
    @classmethod
    def _address(cls, v: str) -> str:
        return validate_address(v)

    @field_validator("platform")
    @classmethod
    def _platform(cls, v: str) -> str:
        if v not in PLATFORMS:
            raise ValueError(f"unknown platform; choose one of {', '.join(PLATFORMS)}")
        return v

    @field_validator("site")
    @classmethod
    def _site(cls, v: str) -> str:
        return v.strip()


class DeviceOut(BaseModel):
    id: int
    name: str
    address: str
    platform: str
    platform_label: str
    site: str
    credential_id: int | None
    credential_name: str | None
    enabled: bool
    notes: str
    created_at: datetime
    updated_at: datetime


def device_out(d: Device) -> DeviceOut:
    return DeviceOut(
        id=d.id, name=d.name, address=d.address, platform=d.platform,
        platform_label=PLATFORMS[d.platform].label if d.platform in PLATFORMS else d.platform,
        site=d.site, credential_id=d.credential_id,
        credential_name=d.credential.name if d.credential else None,
        enabled=d.enabled, notes=d.notes, created_at=d.created_at, updated_at=d.updated_at,
    )


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [device_out(d) for d in db.scalars(select(Device).order_by(Device.name))]


@router.get("/devices/{device_id}", response_model=DeviceOut)
def get_device(device_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return device_out(db.get(Device, device_id) or _not_found("Device"))


@router.post("/devices", response_model=DeviceOut, status_code=201)
def create_device(body: DeviceIn, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    _check_credential(db, body.credential_id)
    device = Device(**body.model_dump())
    db.add(device)
    _flush_unique(db, "A device with that name already exists")
    db.refresh(device)
    audit(db, user.username, "device.create", f"{device.name} ({device.address})")
    return device_out(device)


@router.put("/devices/{device_id}", response_model=DeviceOut)
def update_device(device_id: int, body: DeviceIn, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    device = db.get(Device, device_id) or _not_found("Device")
    _check_credential(db, body.credential_id)
    for key, value in body.model_dump().items():
        setattr(device, key, value)
    _flush_unique(db, "A device with that name already exists")
    db.refresh(device)
    audit(db, user.username, "device.update", device.name)
    return device_out(device)


@router.delete("/devices/{device_id}", status_code=204)
def delete_device(device_id: int, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    device = db.get(Device, device_id) or _not_found("Device")
    db.delete(device)
    audit(db, user.username, "device.delete", device.name)
    return Response(status_code=204)


# --- Audit log --------------------------------------------------------------

@router.get("/audit")
def list_audit(limit: int = 200, db: Session = Depends(get_db),
               _: User = Depends(require_admin)):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 1000)))
    return [{"timestamp": r.timestamp, "username": r.username, "action": r.action,
             "detail": r.detail} for r in rows]


# --- helpers ----------------------------------------------------------------

def _not_found(what: str):
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


def _check_credential(db: Session, cred_id: int | None) -> None:
    if cred_id is not None and db.get(Credential, cred_id) is None:
        raise HTTPException(422, "Credential does not exist")


def _flush_unique(db: Session, message: str) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, message) from None
