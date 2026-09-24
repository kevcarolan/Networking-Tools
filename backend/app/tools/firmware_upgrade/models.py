"""Firmware tool tables. They live in the shared database next to the device
inventory; every table is prefixed ``fw_``."""

from datetime import datetime

from sqlalchemy import (BigInteger, DateTime, ForeignKey, Integer, String, Text,
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
