"""Firmware tool tables. They live in the shared database next to the device
inventory; every table is prefixed ``fw_``."""

from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow

SUCCESS, FAILED, RUNNING, NEVER = "success", "failed", "running", "never"


class DeviceFacts(Base):
    """What is running on a device, from its latest `show version` check."""

    __tablename__ = "fw_device_facts"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    last_status: Mapped[str] = mapped_column(String(20), default=NEVER)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(100), nullable=True)
    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    image: Mapped[str | None] = mapped_column(String(300), nullable=True)
    boot_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)  # install/bundle
    ha_role: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flash_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    flash_free: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    previous_version: Mapped[str | None] = mapped_column(String(100), nullable=True)


class FirmwareImage(Base):
    """An image file in the library (the file itself is in data/firmware/)."""

    __tablename__ = "fw_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(200), unique=True)
    platform: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(100))
    model_pattern: Mapped[str] = mapped_column(String(100), default="*")
    size: Mapped[int] = mapped_column(BigInteger)
    md5: Mapped[str] = mapped_column(String(32))
    sha512: Mapped[str] = mapped_column(String(128))
    verified: Mapped[bool] = mapped_column(default=False)  # matched a vendor checksum on upload
    notes: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(200))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FirmwareStandard(Base):
    """The approved ("golden") version for a platform and model pattern."""

    __tablename__ = "fw_standards"
    __table_args__ = (UniqueConstraint("platform", "model_pattern"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(50))
    model_pattern: Mapped[str] = mapped_column(String(100), default="*")
    target_version: Mapped[str] = mapped_column(String(100))
    image_id: Mapped[int | None] = mapped_column(
        ForeignKey("fw_images.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --- Upgrade jobs -------------------------------------------------------------
# One job per device (or per pair of devices for an HA path). The procedure is a list
# of steps from the platform's driver; the job remembers which step runs next.
#
#   planned -> pre_checking -> blocked | ready -> [staging -> ready] -> running
#     -> completed | completed_overrides | failed | paused (between steps, e.g. a
#        post-check failure that was overridden, or "stop after this step")
#   failed / paused -> (re-run post-checks, override, continue, roll back)
#   + cancelled, rolling_back, rolled_back, needs_attention (interrupted or unclear)
(PLANNED, PRE_CHECKING, BLOCKED, READY, STAGING, RUNNING, POST_CHECKING, PAUSED, COMPLETED,
 COMPLETED_OVERRIDES, FAILED, CANCELLED, ROLLING_BACK, ROLLED_BACK, NEEDS_ATTENTION) = (
    "planned", "pre_checking", "blocked", "ready", "staging", "running", "post_checking",
    "paused", "completed", "completed_overrides", "failed", "cancelled", "rolling_back",
    "rolled_back", "needs_attention")
BUSY_STATES = (PRE_CHECKING, STAGING, RUNNING, POST_CHECKING, ROLLING_BACK)
CLOSED_STATES = (COMPLETED, COMPLETED_OVERRIDES, CANCELLED, ROLLED_BACK)

# Unit (device within a job) progress
U_PENDING, U_ACTIVATED, U_CHECKED, U_FAILED, U_ROLLED_BACK = (
    "pending", "activated", "checked", "failed", "rolled_back")


class DeviceUpgradeSettings(Base):
    """Per-device upgrade settings: the privileged account, the usual upgrade path,
    the HA/pair partner and (FTD) the pinned FDM certificate."""

    __tablename__ = "fw_device_settings"

    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    upgrade_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="RESTRICT"), nullable=True)
    default_path: Mapped[str] = mapped_column(String(50), default="")
    peer_device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    fdm_fingerprint: Mapped[str] = mapped_column(String(100), default="")  # SHA-256, hex
    file_system: Mapped[str] = mapped_column(String(30), default="")  # override, e.g. "bootflash:"


class ImageInfo(Base):
    """Extra repository details for an image (a separate table so existing
    databases get it without a migration)."""

    __tablename__ = "fw_image_info"

    image_id: Mapped[int] = mapped_column(
        ForeignKey("fw_images.id", ondelete="CASCADE"), primary_key=True)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    release_ref: Mapped[str] = mapped_column(String(500), default="")


class UpgradeJob(Base):
    __tablename__ = "fw_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), index=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("fw_images.id", ondelete="RESTRICT"))
    path: Mapped[str] = mapped_column(String(50), default="")  # driver path id
    status: Mapped[str] = mapped_column(String(30), default=PLANNED, index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    from_version: Mapped[str] = mapped_column(String(100), default="")
    target_version: Mapped[str] = mapped_column(String(100))
    change_ref: Mapped[str] = mapped_column(String(100), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    planned_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    planned_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome_note: Mapped[str] = mapped_column(Text, default="")
    pre_backup_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    post_backup_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_run: Mapped[int] = mapped_column(Integer, default=0)   # latest pre-check run number
    post_run: Mapped[int] = mapped_column(Integer, default=0)  # latest post-check run number
    stage_run: Mapped[int] = mapped_column(Integer, default=0)
    # Procedure progress
    plan: Mapped[str] = mapped_column(Text, default="[]")  # JSON: step list from the driver
    step_index: Mapped[int] = mapped_column(Integer, default=0)  # next step to run
    current_step: Mapped[str] = mapped_column(String(200), default="")
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # IOS-XE: activated but not committed yet; reverts by itself at abort_deadline
    pending_commit: Mapped[bool] = mapped_column(Boolean, default=False)
    abort_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobUnit(Base):
    """A device in a job: one for a standalone upgrade, two for a pair."""

    __tablename__ = "fw_job_units"
    __table_args__ = (UniqueConstraint("job_id", "device_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)  # 0 = the job's own device
    role: Mapped[str] = mapped_column(String(50), default="")  # e.g. active / standby
    status: Mapped[str] = mapped_column(String(20), default=U_PENDING)
    from_version: Mapped[str] = mapped_column(String(100), default="")
    running_image: Mapped[str] = mapped_column(String(300), default="")  # for rollback
    rollback_ref: Mapped[str] = mapped_column(String(100), default="")  # e.g. IOS-XE rollback id
    staged: Mapped[bool] = mapped_column(Boolean, default=False)  # image on device, MD5 verified
    image_present: Mapped[bool] = mapped_column(Boolean, default=False)  # file seen in 'dir'


class JobCheck(Base):
    __tablename__ = "fw_job_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the unit
    phase: Mapped[str] = mapped_column(String(10))  # pre / stage / post
    run_no: Mapped[int] = mapped_column(Integer)
    check_id: Mapped[str] = mapped_column(String(50))
    label: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(10))  # blocker / warning / info
    status: Mapped[str] = mapped_column(String(10))    # pass / fail / warn / skip / error
    value: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    overridden_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobSnapshot(Base):
    """Device state captured before and after, for the comparison."""

    __tablename__ = "fw_job_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phase: Mapped[str] = mapped_column(String(10))
    run_no: Mapped[int] = mapped_column(Integer)
    data: Mapped[str] = mapped_column(Text)  # JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobEvent(Base):
    __tablename__ = "fw_job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info / warn / error / action
    step: Mapped[str] = mapped_column(String(50), default="")
    message: Mapped[str] = mapped_column(Text)


class JobCircuits(Base):
    """The affected circuits, frozen when the job is planned and when it starts."""

    __tablename__ = "fw_job_circuits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(10))  # planned / started
    source: Mapped[str] = mapped_column(String(300), default="")  # circuit list import
    count: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[str] = mapped_column(Text)  # JSON: groups as returned by circuits for_device
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- The upgrade worker --------------------------------------------------------------
# The web app never talks to devices for a job: it queues a request and the separate
# worker process (app.worker) carries it out.

class JobRequest(Base):
    __tablename__ = "fw_job_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("fw_jobs.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    username: Mapped[str] = mapped_column(String(200))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    claimed_by: Mapped[str] = mapped_column(String(100), default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str] = mapped_column(Text, default="")


class WorkerStatus(Base):
    """One row, kept up to date by the running worker (its heartbeat)."""

    __tablename__ = "fw_worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    pid: Mapped[int] = mapped_column(Integer, default=0)
    host: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    beat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step: Mapped[str] = mapped_column(String(200), default="")
