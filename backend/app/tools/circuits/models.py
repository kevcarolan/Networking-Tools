"""Master circuit list tables (prefix ``ct_``). Every upload is kept as an
import; exactly one import is "active" and is the current circuit list."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, utcnow

PENDING, ACTIVE, SUPERSEDED = "pending", "active", "superseded"


class CircuitImport(Base):
    __tablename__ = "ct_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    sheet: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(20), default=PENDING, index=True)
    uploaded_by: Mapped[str] = mapped_column(String(200))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    committed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="{}")  # JSON: columns, diff, samples


class Circuit(Base):
    """One row of the circuit list: an end device and the switch port it uses."""

    __tablename__ = "ct_circuits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("ct_imports.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)  # row number in the spreadsheet

    # SERVICE
    ip_partition: Mapped[str] = mapped_column(String(200), default="")
    service: Mapped[str] = mapped_column(String(200), default="")
    # DEVICE INFORMATION
    eng_prefix: Mapped[str] = mapped_column(String(255), default="")
    device_name: Mapped[str] = mapped_column(String(255), default="")
    device_type: Mapped[str] = mapped_column(String(255), default="")
    manufacturer: Mapped[str] = mapped_column(String(255), default="")
    mac: Mapped[str] = mapped_column(String(100), default="")
    # LOCATION
    floor: Mapped[str] = mapped_column(String(200), default="")
    room: Mapped[str] = mapped_column(String(200), default="")
    # IP DETAILS
    ip: Mapped[str] = mapped_column(String(100), default="")
    mask: Mapped[str] = mapped_column(String(100), default="")
    gateway: Mapped[str] = mapped_column(String(100), default="")
    # NETWORK PORT
    vlan: Mapped[str] = mapped_column(String(50), default="")
    conn_type: Mapped[str] = mapped_column(String(100), default="")
    port: Mapped[str] = mapped_column(String(100), default="")
    switch: Mapped[str] = mapped_column(String(255), default="")
    # DRAWING DETAILS
    drawing_number: Mapped[str] = mapped_column(String(255), default="")
    drawing_ref: Mapped[str] = mapped_column(String(255), default="")

    # Derived, for matching
    switch_norm: Mapped[str] = mapped_column(String(255), default="", index=True)
    port_norm: Mapped[str] = mapped_column(String(100), default="")
    mac_norm: Mapped[str] = mapped_column(String(20), default="")
    warnings: Mapped[str] = mapped_column(Text, default="")  # "; "-separated
