"""Everything an upgrade does to a device goes through DeviceIO: show commands,
changes (exec commands that may prompt or reload, config lines), copying the image,
"is it back yet?", and the FDM REST API for FTD.

The upgrade worker gets one DeviceIO; the tests give it a simulator instead
(tests/fake_network.py), so the procedures are tested without real devices.
"""

import logging
import re
import socket
import time
from pathlib import Path

from app.core.platforms import PLATFORMS
from app.core.ssh import ERROR, SETUP, TIMEOUT, DeviceError, Target, run_commands
from app.tools.firmware_upgrade import fdm

log = logging.getLogger(__name__)

# Questions a device may ask during an upgrade command, and the answer we give.
# Checked against the newest output only, so a question is never answered twice.
PROMPT_ANSWERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Save\?\s*\[yes/no\]:?\s*$", re.I), "yes"),
    (re.compile(r"\[yes/no\]:?\s*$", re.I), "yes"),
    (re.compile(r"\(y/n\)\s*\??\s*(\[\w\])?\s*$", re.I), "y"),
    (re.compile(r"\[y/n\]:?\s*$", re.I), "y"),
    (re.compile(r"Do you want to continue.*\?\s*$", re.I), "y"),
    (re.compile(r"reboot the system now\?.*$", re.I), "y"),
    (re.compile(r"Proceed with reload\?.*$", re.I), ""),
    (re.compile(r"Destination filename \[.*\]\?\s*$"), ""),
    (re.compile(r"\[confirm\]\s*$"), ""),
]
RELOAD_CLOSED = "\n[connection closed - the device is reloading]"


class DeviceIO:
    def __init__(self, collector=run_commands):
        self.collector = collector

    # --- read-only ---------------------------------------------------------------

    def show(self, target: Target, commands: list[str]) -> list[str]:
        return self.collector(target, list(commands))

    def reachable(self, target: Target, timeout: float = 5) -> bool:
        """Does the device accept a TCP connection on its management port?"""
        port = 443 if target.platform == "cisco_ftd" else 22
        try:
            with socket.create_connection((target.address, port), timeout=timeout):
                return True
        except OSError:
            return False

    # --- changes ----------------------------------------------------------------

    def _connect(self, target: Target):
        from netmiko import ConnectHandler

        platform = PLATFORMS.get(target.platform)
        if platform is None:
            raise DeviceError(SETUP, f"Unsupported platform '{target.platform}'")
        conn = ConnectHandler(
            device_type=platform.netmiko_type, host=target.address, username=target.username,
            password=target.password, secret=target.enable_secret or "",
            conn_timeout=target.ssh_timeout, auth_timeout=target.ssh_timeout,
            banner_timeout=target.ssh_timeout, fast_cli=False)
        if platform.needs_enable and not conn.check_enable_mode():
            conn.enable()
        return conn

    def change(self, target: Target, commands: list[str], *, config: bool = False,
               timeout: float = 600, expect_reload: bool = False) -> str:
        """Run exec commands (answering the device's questions) or, with config=True,
        configuration lines. With expect_reload, the connection closing is success."""
        conn = self._connect(target)
        try:
            if config:
                return conn.send_config_set(list(commands), read_timeout=timeout)
            return "\n".join(_interactive(conn, cmd, timeout, expect_reload) for cmd in commands)
        finally:
            try:
                conn.disconnect()
            except Exception:  # noqa: BLE001 - already gone after a reload
                pass

    def transfer(self, target: Target, local: Path, file_system: str, filename: str,
                 timeout: float = 3600) -> str:
        """Copy the image to the device over SCP. The checksum is verified separately."""
        if target.platform == "allied_awplus":
            return _scp_put(target, local, f"{file_system}{filename}" if file_system else filename,
                            timeout)
        from netmiko import file_transfer

        conn = self._connect(target)
        try:
            result = file_transfer(conn, source_file=str(local), dest_file=filename,
                                   file_system=file_system or None, direction="put",
                                   overwrite_file=True, disable_md5=True, verify_file=False,
                                   socket_timeout=60)
            return "copied" if result.get("file_transferred") else "already on the device"
        finally:
            conn.disconnect()

    # --- FTD (FDM REST API) -------------------------------------------------------

    def fdm(self, target: Target, fingerprint: str) -> "fdm.FdmClient":
        return fdm.FdmClient(target.address, target.username, target.password, fingerprint)

    def fetch_fingerprint(self, address: str) -> str:
        return fdm.fetch_fingerprint(address)


def _interactive(conn, command: str, timeout: float, expect_reload: bool) -> str:
    """Send one exec command, answer its questions, and return when the prompt is
    back (or, if a reload is expected, when the device drops the connection)."""
    prompt = re.compile(re.escape(conn.base_prompt) + r"[#>]\s*$")
    conn.write_channel(command + conn.RETURN)
    out, seen, deadline = "", 0, time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = conn.read_channel()
            alive = conn.remote_conn.get_transport().is_active()
        except (OSError, EOFError, AttributeError) as exc:
            if expect_reload:
                return out + RELOAD_CLOSED
            raise DeviceError(ERROR, f"Connection lost during '{command}': {exc}") from None
        if not alive and not chunk:
            if expect_reload:
                return out + RELOAD_CLOSED
            raise DeviceError(ERROR, f"Connection closed during '{command}'")
        if not chunk:
            time.sleep(0.5)
            continue
        out += chunk
        new = out[seen:]
        for question, answer in PROMPT_ANSWERS:
            if question.search(new):
                log.info("Answering %r to %r", answer, new[-80:])
                conn.write_channel(answer + conn.RETURN)
                seen = len(out)
                break
        else:
            body = out[len(command):] if out.startswith(command) else out
            if prompt.search(body[-200:]) and len(body.strip()) > len(conn.base_prompt) + 1:
                return out
    if expect_reload:
        raise DeviceError(TIMEOUT, f"'{command}' didn't finish or reload within "
                                   f"{int(timeout // 60)} minutes")
    raise DeviceError(TIMEOUT, f"'{command}' didn't finish within {int(timeout // 60)} minutes")


def _scp_put(target: Target, local: Path, remote: str, timeout: float) -> str:
    """Plain SCP upload (AlliedWare Plus: needs 'ssh server scp')."""
    import paramiko
    from scp import SCPClient

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # noqa: S507 - same as netmiko
    client.connect(target.address, username=target.username, password=target.password,
                   timeout=target.ssh_timeout, look_for_keys=False, allow_agent=False)
    try:
        with SCPClient(client.get_transport(), socket_timeout=60) as scp:
            scp.put(str(local), remote_path=remote)
        return "copied"
    finally:
        client.close()
