"""Runs backups: one-off (manual) and on each device's schedule."""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.db import session_scope, utcnow
from app.core.models import Device
from app.tools.config_backup.collector import (SETUP, BackupError, Target, classify_error,
                                               clean_config, fetch_config)
from app.tools.config_backup.models import (FAILED, NEVER, RUNNING, SUCCESS, BackupRun,
                                            BackupState)
from app.tools.config_backup.storage import GitConfigStore, config_path_for

log = logging.getLogger(__name__)

Fetcher = Callable[[Target], str]


def ensure_states(db: Session, default_frequency: int) -> None:
    """Create a BackupState row for any device that doesn't have one yet.

    Safe to call concurrently (API requests and backup workers both do)."""
    missing = db.scalars(
        select(Device.id).where(~Device.id.in_(select(BackupState.device_id)))
    ).all()
    if missing:
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        db.execute(insert(BackupState).values(
            [{"device_id": device_id, "frequency_minutes": default_frequency,
              "enabled": True, "last_status": NEVER, "consecutive_failures": 0}
             for device_id in missing]
        ).on_conflict_do_nothing())


class BackupService:
    def __init__(self, settings: Settings, cipher: CredentialCipher,
                 store: GitConfigStore, fetcher: Fetcher = fetch_config):
        self.settings = settings
        self.cipher = cipher
        self.store = store
        self.fetcher = fetcher
        self._pool = ThreadPoolExecutor(max_workers=settings.backup_workers,
                                        thread_name_prefix="backup")
        self._in_flight: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = None

    # --- scheduling ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="backup-scheduler", daemon=True)
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
                log.exception("Scheduler tick failed")
            self._stop.wait(self.settings.scheduler_tick_seconds)

    def due_device_ids(self) -> list[int]:
        now = utcnow()
        retry = self.settings.failure_retry_minutes
        with session_scope() as db:
            ensure_states(db, self.settings.default_frequency_minutes)
            rows = db.execute(
                select(BackupState, Device.enabled).join(Device, Device.id == BackupState.device_id)
            ).all()
            due = []
            for state, device_enabled in rows:
                if not (device_enabled and state.enabled) or state.last_status == RUNNING:
                    continue
                if state.last_attempt is None:
                    due.append(state.device_id)
                    continue
                interval = state.frequency_minutes
                if state.last_status == FAILED:
                    interval = min(interval, retry)
                if state.last_attempt + timedelta(minutes=interval) <= now:
                    due.append(state.device_id)
            return due

    def tick(self) -> int:
        """Queue every device that is due. Returns how many were queued."""
        queued = sum(self.submit(device_id, "schedule") for device_id in self.due_device_ids())
        self._prune_history()
        return queued

    def submit(self, device_id: int, trigger: str) -> bool:
        """Queue a backup in the worker pool. False if one is already queued/running."""
        with self._lock:
            if device_id in self._in_flight:
                return False
            self._in_flight.add(device_id)
        self._pool.submit(self._run_and_release, device_id, trigger)
        return True

    def is_busy(self, device_id: int) -> bool:
        with self._lock:
            return device_id in self._in_flight

    def _run_and_release(self, device_id: int, trigger: str) -> None:
        try:
            self.run(device_id, trigger)
        except Exception:  # noqa: BLE001
            log.exception("Backup of device %s crashed", device_id)
        finally:
            with self._lock:
                self._in_flight.discard(device_id)

    def _prune_history(self) -> None:
        now = utcnow()
        if self._last_prune and now - self._last_prune < timedelta(hours=1):
            return
        self._last_prune = now
        cutoff = now - timedelta(days=self.settings.history_retention_days)
        with session_scope() as db:
            db.execute(delete(BackupRun).where(BackupRun.started_at < cutoff))

    # --- running one backup -------------------------------------------------

    def run(self, device_id: int, trigger: str = "manual") -> int | None:
        """Back up one device synchronously. Returns the BackupRun id."""
        # 1. Record the attempt and snapshot what we need, without holding a
        #    DB session open during the (slow) SSH session.
        with session_scope() as db:
            device = db.get(Device, device_id)
            if device is None:
                return None
            ensure_states(db, self.settings.default_frequency_minutes)
            state = db.get(BackupState, device_id)
            run = BackupRun(device_id=device_id, trigger=trigger, status=RUNNING)
            db.add(run)
            state.last_attempt = utcnow()
            state.last_status = RUNNING
            db.flush()
            run_id = run.id
            name, platform, site = device.name, device.platform, device.site
            previous_path = state.config_path
            target = None
            setup_error = None
            if device.credential is None:
                setup_error = BackupError(SETUP, "No credential profile assigned to this device")
            else:
                try:
                    target = Target(
                        name=name, address=device.address, platform=platform,
                        username=device.credential.username,
                        password=self.cipher.decrypt(device.credential.password_enc),
                        enable_secret=self.cipher.decrypt(device.credential.enable_secret_enc)
                        if device.credential.enable_secret_enc else None,
                        ssh_timeout=self.settings.ssh_timeout,
                        command_timeout=self.settings.command_timeout,
                    )
                except Exception:  # noqa: BLE001 - e.g. credential key was replaced
                    setup_error = BackupError(
                        SETUP, "Stored credential could not be decrypted - re-enter the password")

        # 2. Fetch, clean and store the config.
        changed, commit, rel_path = False, None, config_path_for(site, name)
        error_type, message = None, ""
        try:
            if setup_error:
                raise setup_error
            config = clean_config(platform, self.fetcher(target))
            who = "scheduled backup" if trigger == "schedule" else f"backup ({trigger})"
            changed, commit = self.store.save(rel_path, config, f"{name}: {who}",
                                              previous_path=previous_path)
            message = "Configuration changed" if changed else "No changes"
        except Exception as exc:  # noqa: BLE001
            error_type, message = classify_error(exc)
            log.warning("Backup of %s failed: %s", name, message)

        # 3. Record the outcome.
        now = utcnow()
        with session_scope() as db:
            run = db.get(BackupRun, run_id)
            state = db.get(BackupState, device_id)
            if run is None or state is None:  # device deleted mid-backup
                return None
            run.finished_at = now
            run.status = FAILED if error_type else SUCCESS
            run.changed, run.commit = changed, commit
            run.error_type, run.message = error_type, message
            state.last_status = run.status
            if error_type:
                state.consecutive_failures += 1
                state.last_error_type, state.last_error = error_type, message
            else:
                state.consecutive_failures = 0
                state.last_error_type = state.last_error = None
                state.last_success = now
                state.config_path, state.last_commit = rel_path, commit
                if changed:
                    state.last_change = now
        return run_id

    def recover_interrupted(self) -> None:
        """Mark runs left 'running' by a previous crash/restart as failed."""
        with session_scope() as db:
            for run in db.scalars(select(BackupRun).where(BackupRun.status == RUNNING)):
                run.status, run.error_type = FAILED, "error"
                run.message, run.finished_at = "Interrupted by a service restart", utcnow()
            for state in db.scalars(select(BackupState).where(BackupState.last_status == RUNNING)):
                state.last_status = FAILED
                state.last_error_type = "error"
                state.last_error = "Interrupted by a service restart"
