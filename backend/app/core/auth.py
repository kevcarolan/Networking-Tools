"""Login against Active Directory (LDAP), with an optional local break-glass admin.

Roles come from AD group membership (nested groups included):
  * admin  - member of NETOPS_LDAP_ADMIN_GROUP: can add/edit/delete and run backups
  * viewer - member of NETOPS_LDAP_VIEWER_GROUP (or any domain user if unset): read-only
"""

import logging
import ssl
import threading
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.config import Settings
from app.core.crypto import verify_password
from app.core.db import get_db

log = logging.getLogger(__name__)

ADMIN, VIEWER = "admin", "viewer"
_IN_CHAIN = "1.2.840.113556.1.4.1941"  # AD rule that matches nested group membership


@dataclass
class User:
    username: str
    role: str
    display_name: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


class LoginThrottle:
    """Blocks a username/address pair after repeated failed logins."""

    def __init__(self, max_failures: int = 5, window_seconds: int = 300):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        self._failures[key] = recent
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class Authenticator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def authenticate(self, username: str, password: str) -> User | None:
        # An empty password would be an anonymous (always successful) LDAP bind.
        if not username or not password:
            return None
        s = self.settings
        if s.local_admin_password_hash and username == s.local_admin_user:
            if verify_password(password, s.local_admin_password_hash):
                return User(username=username, role=ADMIN, display_name="Local administrator")
            return None
        if s.ldap_url:
            try:
                return self._ldap_authenticate(username, password)
            except Exception:  # noqa: BLE001 - never leak LDAP errors to the client
                log.exception("LDAP authentication error for %s", username)
        return None

    def _ldap_authenticate(self, username: str, password: str) -> User | None:
        from ldap3 import NONE, SIMPLE, Connection, Server, Tls
        from ldap3.utils.conv import escape_filter_chars

        s = self.settings
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=s.ldap_ca_file or None)
        server = Server(
            s.ldap_url, use_ssl=s.ldap_url.lower().startswith("ldaps://"),
            tls=tls, get_info=NONE, connect_timeout=5,
        )
        sam = username.split("\\")[-1].split("@")[0]
        principal = username if ("@" in username or "\\" in username) else f"{sam}@{s.ldap_domain}"
        conn = Connection(server, user=principal, password=password,
                          authentication=SIMPLE, receive_timeout=10)
        conn.open()
        if not s.ldap_url.lower().startswith("ldaps://"):
            conn.start_tls()  # never send passwords in clear text
        if not conn.bind():
            return None
        try:
            user_filter = f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(sam)}))"

            def in_group(group_dn: str) -> bool:
                flt = (f"(&{user_filter}"
                       f"(memberOf:{_IN_CHAIN}:={escape_filter_chars(group_dn)}))")
                conn.search(s.ldap_base_dn, flt, attributes=["sAMAccountName"])
                return bool(conn.entries)

            conn.search(s.ldap_base_dn, user_filter, attributes=["displayName"])
            if not conn.entries:
                return None
            display = str(conn.entries[0].displayName or sam)

            if s.ldap_admin_group and in_group(s.ldap_admin_group):
                return User(username=sam, role=ADMIN, display_name=display)
            if not s.ldap_viewer_group or in_group(s.ldap_viewer_group):
                return User(username=sam, role=VIEWER, display_name=display)
            return None
        finally:
            conn.unbind()


# --- FastAPI dependencies -------------------------------------------------

def current_user(request: Request) -> User:
    data = request.session.get("user")
    if not data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in")
    return User(**data)


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    return user


# --- Routes ----------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    authenticator: Authenticator = request.app.state.authenticator
    throttle: LoginThrottle = request.app.state.login_throttle
    client = request.client.host if request.client else "unknown"
    key = f"{body.username.lower()}|{client}"
    if throttle.blocked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed logins, try later")
    user = authenticator.authenticate(body.username.strip(), body.password)
    if user is None:
        throttle.fail(key)
        audit(db, body.username.strip()[:200], "login.failed", f"from {client}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    throttle.reset(key)
    request.session.clear()
    request.session["user"] = user.__dict__
    audit(db, user.username, "login", f"role={user.role} from {client}")
    return user.__dict__


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user.__dict__
