"""Fetches running configs from devices over SSH (via Netmiko) and cleans them."""

from app.core.platforms import PLATFORMS, Platform
from app.core.ssh import (AUTH, COMMAND, ERROR, SETUP, TIMEOUT, UNREACHABLE,  # noqa: F401
                          Target, classify_error, run_commands)
from app.core.ssh import DeviceError as BackupError

_CLI_ERRORS = ("% Invalid input", "% Incomplete command", "% Unknown command",
               "ERROR: % Invalid", "% Ambiguous command")


def fetch_config(target: Target) -> str:
    """Log in to the device and return the raw output of its backup command(s)."""
    platform = PLATFORMS.get(target.platform)
    if platform is None:
        raise BackupError(SETUP, f"Unsupported platform '{target.platform}'")
    return "\n".join(run_commands(target, platform.backup_commands))


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
