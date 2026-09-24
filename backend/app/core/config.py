"""Application settings, read from environment variables prefixed ``NETOPS_``
(or from a ``.env`` file in the working directory)."""

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NETOPS_", env_file=".env", extra="ignore")

    # Storage: SQLite database and the git repository of device configs live here.
    data_dir: Path = REPO_ROOT / "data"
    # Key used to encrypt device credentials. Keep it OUTSIDE data_dir in
    # production (e.g. /etc/netops/credential.key) and back it up separately.
    credential_key_file: Path | None = None
    # Key used to sign login session cookies. Generated on first run if unset.
    secret_key: str = ""

    # Web sessions
    session_https_only: bool = False
    session_max_age_hours: int = 12

    # Active Directory / LDAP
    ldap_url: str = ""  # e.g. ldaps://dc1.corp.local
    ldap_domain: str = ""  # e.g. corp.local  (users log in as "jsmith")
    ldap_base_dn: str = ""  # e.g. DC=corp,DC=local
    ldap_admin_group: str = ""  # DN of the group allowed to make changes
    ldap_viewer_group: str = ""  # DN of the read-only group; empty = any domain user
    ldap_ca_file: str = ""  # CA bundle for validating the DC certificate

    # Local break-glass admin (works even if AD is down). Leave the hash empty to disable.
    local_admin_user: str = "admin"
    local_admin_password_hash: str = ""

    # Config backup tool
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 30
    backup_workers: int = 10
    default_frequency_minutes: int = 1440
    failure_retry_minutes: int = 60
    ssh_timeout: int = 30
    command_timeout: int = 180
    history_retention_days: int = 90
    viewers_can_read_configs: bool = False

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'app.db'}"

    @property
    def configs_dir(self) -> Path:
        return self.data_dir / "configs"

    def prepare(self) -> "Settings":
        """Create the data directory and fill in generated secrets."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.credential_key_file is None:
            self.credential_key_file = self.data_dir / "credential.key"
        if not self.secret_key:
            self.secret_key = _read_or_create_secret(self.data_dir / "session.key")
        return self


def _read_or_create_secret(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(48)
    _write_private(path, value)
    return value


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)


@lru_cache
def get_settings() -> Settings:
    return Settings().prepare()
