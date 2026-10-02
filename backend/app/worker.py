"""The upgrade worker: carries out upgrade job requests queued by the web app.

    python -m app.worker          (systemd: netops-worker.service)

It is a separate process so that restarting or upgrading the web app never interrupts
a copy or a reload. It keeps a heartbeat row (fw_worker_status) that the GUI and PRTG
read. On start it marks anything that was mid-step "needs attention" - it never
resumes a step blindly. On shutdown (SIGTERM) running jobs pause before their next
step; the systemd unit gives them time to finish the current one.
"""

import logging
import os
import signal
import socket
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.core.crypto import CredentialCipher
from app.core.db import init_engine, session_scope, utcnow
from app.tools.config_backup.collector import fetch_config
from app.tools.config_backup.service import BackupService
from app.tools.config_backup.storage import GitConfigStore
from app.tools.firmware_upgrade.deviceio import DeviceIO
from app.tools.firmware_upgrade.jobs import JobService
from app.tools.firmware_upgrade.models import JobRequest, WorkerStatus

log = logging.getLogger("netops.worker")


class UpgradeWorker:
    def __init__(self, settings: Settings, service: JobService):
        self.settings = settings
        self.service = service
        self.name = f"{socket.gethostname()}:{os.getpid()}"
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.upgrade_workers),
                                        thread_name_prefix="upgrade")
        self._running: dict[int, Future] = {}  # job id -> future
        self._steps: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        service.on_step = self._on_step

    # --- heartbeat ----------------------------------------------------------------

    def _on_step(self, job_id: int, label: str) -> None:
        with self._lock:
            self._steps[job_id] = label

    def heartbeat(self) -> None:
        with self._lock:
            running = {j: s for j, s in self._steps.items() if j in self._running}
        with session_scope() as db:
            w = db.get(WorkerStatus, 1)
            if w is None:
                w = WorkerStatus(id=1, started_at=utcnow())
                db.add(w)
            w.pid, w.host, w.beat_at = os.getpid(), socket.gethostname(), utcnow()
            first = next(iter(running.items()), (None, ""))
            w.job_id, w.step = first[0], first[1][:200]

    def start(self) -> None:
        self.service.recover_interrupted()
        with session_scope() as db:
            w = db.get(WorkerStatus, 1) or WorkerStatus(id=1)
            w.started_at = utcnow()
            db.add(w)
        self.heartbeat()
        log.info("Upgrade worker %s started", self.name)

    # --- requests -----------------------------------------------------------------

    def _claim(self) -> list[tuple]:
        claimed = []
        with session_scope() as db:
            pending = db.scalars(select(JobRequest).where(JobRequest.claimed_at.is_(None),
                                                          JobRequest.finished_at.is_(None))
                                 .order_by(JobRequest.id)).all()
            for req in pending:
                with self._lock:
                    if req.job_id in self._running or \
                            len(self._running) >= self.settings.upgrade_workers:
                        continue
                won = db.execute(update(JobRequest).where(JobRequest.id == req.id,
                                                          JobRequest.claimed_at.is_(None))
                                 .values(claimed_at=utcnow(), claimed_by=self.name)).rowcount
                if won:
                    claimed.append((req.id, req.job_id, req.action, req.username))
        return claimed

    def process_once(self, wait: bool = False) -> int:
        """Start every request that can run now. Returns how many were started."""
        self._reap()
        started = 0
        for req_id, job_id, action, username in self._claim():
            fut = self._pool.submit(self._run, req_id, job_id, action, username)
            with self._lock:
                self._running[job_id] = fut
            started += 1
        if wait:
            for fut in list(self._running.values()):
                fut.result()
            self._reap()
        return started

    def _run(self, req_id: int, job_id: int, action: str, username: str) -> None:
        log.info("Job %s: %s by %s", job_id, action, username)
        try:
            result = self.service.run(job_id, action, username)
        except Exception as exc:  # noqa: BLE001 - run() records its own failures
            log.exception("Job %s %s crashed", job_id, action)
            result = f"error: {exc}"
        with session_scope() as db:
            req = db.get(JobRequest, req_id)
            if req:
                req.finished_at, req.result = utcnow(), str(result)[:500]

    def _reap(self) -> None:
        with self._lock:
            for job_id in [j for j, f in self._running.items() if f.done()]:
                self._running.pop(job_id)
                self._steps.pop(job_id, None)

    def busy(self) -> bool:
        self._reap()
        with self._lock:
            return bool(self._running)

    # --- main loop ----------------------------------------------------------------

    def run_forever(self) -> None:
        self.start()
        beat_every, last_beat = 5.0, 0.0
        while not self._stop.is_set():
            try:
                self.process_once()
                self.service.watch_abort_timers()
                now = self.service.monotonic()
                if now - last_beat >= beat_every:
                    self.heartbeat()
                    last_beat = now
            except Exception:  # noqa: BLE001 - keep the worker alive (e.g. database locked)
                log.exception("Worker loop error")
            self._stop.wait(self.settings.upgrade_worker_poll_seconds)
        log.info("Stopping: running jobs pause before their next step")
        self.service.stopping = True
        while self.busy():
            self.heartbeat()
            self._stop_wait(5)
        self._pool.shutdown(wait=True)
        log.info("Upgrade worker stopped")

    def _stop_wait(self, seconds: float) -> None:
        threading.Event().wait(seconds)

    def stop(self, *_args) -> None:
        self._stop.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    init_engine(settings.database_url)
    cipher = CredentialCipher(settings.credential_key_file)
    backups = BackupService(settings, cipher, GitConfigStore(settings.configs_dir),
                            fetcher=fetch_config)
    worker = UpgradeWorker(settings, JobService(settings, cipher, backups, io=DeviceIO()))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
