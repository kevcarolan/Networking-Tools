"""NetOps platform entry point.  Run with:  uvicorn --factory app.main:create_app"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core import auth, inventory
from app.core.config import Settings, get_settings
from app.core.crypto import CredentialCipher
from app.core.db import init_engine
from app.tools.config_backup import api as backup_api
from app.tools.config_backup.collector import fetch_config
from app.tools.config_backup.service import BackupService
from app.tools.config_backup.storage import GitConfigStore

STATIC_DIR = Path(__file__).parent / "static"

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(settings: Settings | None = None, fetcher=fetch_config,
               start_scheduler: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_engine(settings.database_url)
    cipher = CredentialCipher(settings.credential_key_file)
    store = GitConfigStore(settings.configs_dir)
    backup_service = BackupService(settings, cipher, store, fetcher=fetcher)
    run_scheduler = settings.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        backup_service.recover_interrupted()
        if run_scheduler:
            backup_service.start()
        yield
        backup_service.stop()

    app = FastAPI(title="NetOps Tools", lifespan=lifespan)
    app.state.settings = settings
    app.state.cipher = cipher
    app.state.authenticator = auth.Authenticator(settings)
    app.state.login_throttle = auth.LoginThrottle()
    app.state.backup_service = backup_service

    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key, session_cookie="netops_session",
        max_age=settings.session_max_age_hours * 3600, same_site="strict",
        https_only=settings.session_https_only,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    app.include_router(auth.router)
    app.include_router(inventory.router)
    app.include_router(backup_api.router)

    @app.get("/api/health", include_in_schema=False)
    def health():
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app

