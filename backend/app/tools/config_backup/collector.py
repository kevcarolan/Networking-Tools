"""Fetches running configs from devices over SSH (via Netmiko) and cleans them."""

import socket
from dataclasses import dataclass

from app.core.platforms import PLATFORMS, Platform

# Error types shown in the GUI
AUTH, UNREACHABLE, TIMEOUT, COMMAND, SETUP, ERROR = (
    "auth", "unreachable", "timeout", "command", "setup", "error")

_CLI_ERRORS = ("% Invalid input", "% Incomplete command", "% Unknown command",
               "ERROR: % Invalid", "% Ambiguous command")


class BackupError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class Target:
    name: str
    address: str
    platform: str
    username: str
    password: str
    enable_secret: str | None
    ssh_timeout: int = 30
    command_timeout: int = 180


def fetch_config(target: Target) -> str:
    """Log in to the device and return the raw output of its backup command(s)."""
    from netmiko import ConnectHandler

    platform = PLATFORMS.get(target.platform)
    if platform is None:
        raise BackupError(SETUP, f"Unsupported platform '{target.platform}'")
    params = dict(
        device_type=platform.netmiko_type, host=target.address,
        username=target.username, password=target.password,
        secret=target.enable_secret or "",
        conn_timeout=target.ssh_timeout, auth_timeout=target.ssh_timeout,
        banner_timeout=target.ssh_timeout, fast_cli=False,
    )
    with ConnectHandler(**params) as conn:
        if platform.needs_enable and not conn.check_enable_mode():
            conn.enable()
        outputs = [conn.send_command(cmd, read_timeout=target.command_timeout)
                   for cmd in platform.backup_commands]
    return "\n".join(outputs)


def clean_config(platform_key: str, raw: str) -> str:
    """Remove volatile lines and normalise whitespace; reject obvious garbage."""
    platform: Platform | None = PLATFORMS.get(platform_key)
    volatile = platform.volatile_regex() if platform else None
    lines = [ln.rstrip() for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    if volatile:
        lines = [ln for ln in lines if not volatile.search(ln)]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    text = "\n".join(lines)
    for marker in _CLI_ERRORS:
        if marker in text[:500]:
            raise BackupError(COMMAND, f"Device rejected the backup command: {marker.strip()}")
    if len(lines) < 5:
        raise BackupError(COMMAND, "Device returned an empty or truncated configuration")
    return text + "\n"


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to (error_type, human-readable message)."""
    if isinstance(exc, BackupError):
        return exc.error_type, str(exc)
    from netmiko.exceptions import (NetmikoAuthenticationException,
                                    NetmikoTimeoutException, ReadTimeout)

    if isinstance(exc, NetmikoAuthenticationException):
        return AUTH, "Authentication failed - check the credential profile"
    if isinstance(exc, ReadTimeout):
        return TIMEOUT, "Timed out waiting for the device to return its configuration"
    if isinstance(exc, NetmikoTimeoutException):
        return UNREACHABLE, "Could not connect over SSH (port 22) - device unreachable?"
    if isinstance(exc, socket.gaierror):
        return UNREACHABLE, f"Hostname could not be resolved: {exc}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return TIMEOUT, "Connection timed out"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return UNREACHABLE, f"Connection failed: {exc}"
    return ERROR, f"{type(exc).__name__}: {exc}"[:1000]
