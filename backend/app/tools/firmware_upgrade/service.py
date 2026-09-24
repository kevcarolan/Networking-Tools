"""Collects software versions from devices: on demand and on a daily schedule."""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.db import session_scope, utcnow
from app.core.models import Device
from app.core.ssh import (COMMAND, SETUP, DeviceError, Target, classify_error, run_commands,
                          target_for)
from app.tools.firmware_upgrade.facts import FACT_COMMANDS, parse_facts
from app.tools.firmware_upgrade.models import FAILED, NEVER, RUNNING, SUCCESS, DeviceFacts

log = logging.getLogger(__name__)

# (target, commands) -> outputs; replaced by a fake in the tests.
Collector = Callable[[Target, tuple[str, ...]], list[str]]


def ensure_facts(db: Session) -> None:
    """Create an empty DeviceFacts row for any device that doesn't have one."""
    missing = db.scalars(
        select(Device.id).where(~Device.id.in_(select(DeviceFacts.device_id)))
    ).all()
    if missing:
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        db.execute(insert(DeviceFacts).values(
            [{"device_id": device_id, "last_status": NEVER} for device_id in missing]
        ).on_conflict_do_nothing())


class FirmwareService:
    def __init__(self, settings: Settings, cipher: CredentialCipher,
                 collector: Collector = run_commands):
        self.settings = settings
        self.cipher = cipher
        self.collector = collector
        self._pool = ThreadPoolExecutor(max_workers=settings.firmware_workers,
                                        thread_name_prefix="firmware")
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- scheduling ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="firmware-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the scheduler alive
                log.exception("Firmware scheduler tick failed")
            self._stop.wait(self.settings.scheduler_tick_seconds)

    def due_device_ids(self) -> list[int]:
        now = utcnow()
        every = timedelta(minutes=self.settings.firmware_check_minutes)
        retry = timedelta(minutes=min(self.settings.failure_retry_minutes,
                                      self.settings.firmware_check_minutes))
        with session_scope() as db:
            ensure_facts(db)
            rows = db.execute(select(DeviceFacts, Device.enabled)
                              .join(Device, Device.id == DeviceFacts.device_id)).all()
            return [f.device_id for f, enabled in rows
                    if enabled and f.last_status != RUNNING and (
                        f.last_attempt is None
                        or f.last_attempt + (retry if f.last_status == FAILED else every) <= now)]

    def tick(self) -> int:
        return sum(self.submit(device_id) for device_id in self.due_device_ids())

    def submit(self, device_id: int) -> bool:
        """Queue a version check. False if one is already queued/running."""
        with self._lock:
            if device_id in self._in_flight:
                return False
            self._in_flight.add(device_id)
        self._pool.submit(self._run_and_release, device_id)
        return True

    def is_busy(self, device_id: int) -> bool:
        with self._lock:
            return device_id in self._in_flight

    def _run_and_release(self, device_id: int) -> None:
        try:
            self.check(device_id)
        except Exception:  # noqa: BLE001
            log.exception("Version check of device %s crashed", device_id)
        finally:
            with self._lock:
                self._in_flight.discard(device_id)

    # --- one check ----------------------------------------------------------

    def check(self, device_id: int) -> str | None:
        """Check one device synchronously. Returns the resulting status."""
        with session_scope() as db:
            device = db.get(Device, device_id)
            if device is None:
                return None
            ensure_facts(db)
            facts_row = db.get(DeviceFacts, device_id)
            facts_row.last_attempt, facts_row.last_status = utcnow(), RUNNING
            platform, name = device.platform, device.name
            target, setup_error = None, None
            try:
                target = target_for(device, self.cipher, self.settings.ssh_timeout,
                                    self.settings.command_timeout)
            except DeviceError as exc:
                setup_error = exc

        facts, error_type, message = None, None, ""
        try:
            if setup_error:
                raise setup_error
            commands = FACT_COMMANDS.get(platform)
            if commands is None:
                raise DeviceError(SETUP, f"Version checks are not supported for '{platform}'")
            outputs = self.collector(target, commands)
            try:
                facts = parse_facts(platform, outputs)
            except ValueError as exc:
                raise DeviceError(COMMAND, str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_error(exc)
            log.warning("Version check of %s failed: %s", name, message)

        now = utcnow()
        with session_scope() as db:
            row = db.get(DeviceFacts, device_id)
            if row is None:  # device deleted mid-check
                return None
            if error_type:
                row.last_status = FAILED
                row.last_error_type, row.last_error = error_type, message
                return FAILED
            if row.version and row.version != facts.version:
                row.previous_version, row.version_changed_at = row.version, now
            row.last_status, row.last_success = SUCCESS, now
            row.last_error_type = row.last_error = None
            for key, value in vars(facts).items():
                setattr(row, key, value)
            return SUCCESS

    def recover_interrupted(self) -> None:
        with session_scope() as db:
            for row in db.scalars(select(DeviceFacts).where(DeviceFacts.last_status == RUNNING)):
                row.last_status, row.last_error_type = FAILED, "error"
                row.last_error = "Interrupted by a service restart"
