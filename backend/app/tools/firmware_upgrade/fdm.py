"""A small client for the FDM REST API (Firepower Threat Defense managed by FDM).

Standard library only. FDM uses a self-signed certificate, so instead of a CA we
pin its SHA-256 fingerprint: an admin confirms the fingerprint once in the device's
upgrade settings, and every connection is refused if it doesn't match.

The endpoint paths are in ENDPOINTS so they can be adjusted to the FDM API version
without touching the procedure (confirm them in the lab - see upgrade-lab-tests.md).
"""

import hashlib
import json
import socket
import ssl
import uuid
from http.client import HTTPSConnection
from pathlib import Path

from app.core.ssh import AUTH, ERROR, SETUP, UNREACHABLE, DeviceError

BASE = "/api/fdm/latest"
ENDPOINTS = {
    "token": "/fdm/token",
    "system": "/operational/systeminfo/default",
    "pending": "/operational/pendingchanges",
    "ha_status": "/devices/default/operational/ha/status/default",
    "ha_failover": "/devices/default/action/ha/failover",
    "upload": "/action/uploadupgrade",
    "upgrade_files": "/managedentity/upgradefiles",
    "readiness": "/action/upgradereadinesscheck",
    "upgrade": "/action/upgrade",
    "upgrade_status": "/managedentity/upgradestatus",
    "revert": "/action/revertupgrade",
}


def _context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # trust comes from the pinned fingerprint instead
    return ctx


def fetch_fingerprint(address: str, port: int = 443, timeout: float = 10) -> str:
    """SHA-256 of the certificate the device presents, as colon-separated hex."""
    try:
        with socket.create_connection((address, port), timeout=timeout) as sock, \
                _context().wrap_socket(sock) as tls:
            der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise DeviceError(UNREACHABLE, f"Could not connect to {address}:{port}: {exc}") from None
    return format_fp(hashlib.sha256(der).hexdigest())


def format_fp(hexdigest: str) -> str:
    h = hexdigest.replace(":", "").lower()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2)).upper()


class FdmClient:
    def __init__(self, address: str, username: str, password: str, fingerprint: str,
                 timeout: float = 60, port: int = 443):
        if not fingerprint:
            raise DeviceError(SETUP, "The FDM certificate isn't trusted yet: an admin must "
                                     "confirm its fingerprint in the device's upgrade settings")
        self.address, self.username, self.password = address, username, password
        self.fingerprint = format_fp(fingerprint)
        self.timeout, self.port = timeout, port
        self.token: str | None = None

    # --- transport ---------------------------------------------------------------

    def _connection(self) -> HTTPSConnection:
        conn = HTTPSConnection(self.address, self.port, timeout=self.timeout, context=_context())
        try:
            conn.connect()
        except OSError as exc:
            raise DeviceError(UNREACHABLE, f"FDM API unreachable: {exc}") from None
        seen = format_fp(hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest())
        if seen != self.fingerprint:
            conn.close()
            raise DeviceError(SETUP, f"FDM certificate changed (now {seen}); check the device "
                                     "and trust the new certificate in its upgrade settings")
        return conn

    def request(self, method: str, name_or_path: str, body=None, raw: bytes | None = None,
                content_type: str = "application/json") -> dict:
        path = BASE + ENDPOINTS.get(name_or_path, name_or_path)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = raw
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif raw is not None:
            headers["Content-Type"] = content_type
        conn = self._connection()
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
        except OSError as exc:
            raise DeviceError(UNREACHABLE, f"FDM API connection failed: {exc}") from None
        finally:
            conn.close()
        if resp.status in (401, 403):
            raise DeviceError(AUTH, "FDM API rejected the upgrade account")
        if resp.status >= 400:
            raise DeviceError(ERROR, f"FDM API {method} {path}: HTTP {resp.status} {text[:300]}")
        return json.loads(text) if text.strip() else {}

    def login(self) -> None:
        r = self.request("POST", "token", {"grant_type": "password", "username": self.username,
                                           "password": self.password})
        self.token = r.get("access_token")
        if not self.token:
            raise DeviceError(AUTH, "FDM API returned no access token")

    # --- read-only ---------------------------------------------------------------

    def system_info(self) -> dict:
        return self.request("GET", "system")

    def pending_changes(self) -> list:
        return self.request("GET", "pending").get("items", [])

    def ha_status(self) -> dict:
        return self.request("GET", "ha_status")

    def upgrade_files(self) -> list:
        return self.request("GET", "upgrade_files").get("items", [])

    def upgrade_status(self) -> dict:
        r = self.request("GET", "upgrade_status")
        items = r.get("items")
        return items[0] if items else r

    # --- changes -----------------------------------------------------------------

    def upload(self, local: Path) -> dict:
        boundary = uuid.uuid4().hex
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; "
                f"filename=\"{local.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n")
        # FTD packages are up to ~1 GB; read in one go (the worker has the memory headroom)
        raw = head.encode() + local.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", "upload", raw=raw,
                            content_type=f"multipart/form-data; boundary={boundary}")

    def readiness_check(self, file_id: str) -> dict:
        return self.request("POST", "readiness", {"type": "upgradereadinesscheck",
                                                  "upgradeFile": {"id": file_id}})

    def start_upgrade(self, file_id: str) -> dict:
        return self.request("POST", "upgrade", {"type": "upgrade", "upgradeFile": {"id": file_id}})

    def ha_failover(self) -> dict:
        return self.request("POST", "ha_failover", {})

    def revert(self) -> dict:
        return self.request("POST", "revert", {})
