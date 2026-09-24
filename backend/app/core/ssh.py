"""SSH access to devices (via Netmiko), shared by every tool: login, running
show commands, and turning connection errors into a type the GUI can show."""

import socket
from dataclasses import dataclass

from app.core.crypto import CredentialCipher
from app.core.models import Device
from app.core.platforms import PLATFORMS

# Error types shown in the GUI
AUTH, UNREACHABLE, TIMEOUT, COMMAND, SETUP, ERROR = (
    "auth", "unreachable", "timeout", "command", "setup", "error")


class DeviceError(Exception):
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


def target_for(device: Device, cipher: CredentialCipher, ssh_timeout: int = 30,
               command_timeout: int = 180) -> Target:
    """Build a Target from a device and its credential profile.

    Raises DeviceError(SETUP) when the device has no usable credential."""
    cred = device.credential
    if cred is None:
        raise DeviceError(SETUP, "No credential profile assigned to this device")
    try:
        password = cipher.decrypt(cred.password_enc)
        enable = cipher.decrypt(cred.enable_secret_enc) if cred.enable_secret_enc else None
    except Exception:  # noqa: BLE001 - e.g. credential key was replaced
        raise DeviceError(
            SETUP, "Stored credential could not be decrypted - re-enter the password") from None
    return Target(name=device.name, address=device.address, platform=device.platform,
                  username=cred.username, password=password, enable_secret=enable,
                  ssh_timeout=ssh_timeout, command_timeout=command_timeout)


def run_commands(target: Target, commands: tuple[str, ...] | list[str]) -> list[str]:
    """Log in (entering enable mode if the platform needs it) and return the
    output of each command, in order."""
    from netmiko import ConnectHandler

    platform = PLATFORMS.get(target.platform)
    if platform is None:
        raise DeviceError(SETUP, f"Unsupported platform '{target.platform}'")
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
        return [conn.send_command(cmd, read_timeout=target.command_timeout) for cmd in commands]


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to (error_type, human-readable message)."""
    if isinstance(exc, DeviceError):
        return exc.error_type, str(exc)
    from netmiko.exceptions import (NetmikoAuthenticationException,
                                    NetmikoTimeoutException, ReadTimeout)

    if isinstance(exc, NetmikoAuthenticationException):
        return AUTH, "Authentication failed - check the credential profile"
    if isinstance(exc, ReadTimeout):
        return TIMEOUT, "Timed out waiting for the device to respond"
    if isinstance(exc, NetmikoTimeoutException):
        return UNREACHABLE, "Could not connect over SSH (port 22) - device unreachable?"
    if isinstance(exc, socket.gaierror):
        return UNREACHABLE, f"Hostname could not be resolved: {exc}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return TIMEOUT, "Connection timed out"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
        return UNREACHABLE, f"Connection failed: {exc}"
    return ERROR, f"{type(exc).__name__}: {exc}"[:1000]
