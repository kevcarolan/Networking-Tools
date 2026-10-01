import logging

from sqlalchemy.orm import Session

from app.core.models import AuditEvent

# Audit events also go to the service log (journald), so they can be forwarded
# to a SIEM and survive someone tampering with the database.
log = logging.getLogger("netops.audit")


def audit(db: Session, username: str, action: str, detail: str = "") -> None:
    db.add(AuditEvent(username=username, action=action, detail=detail))
    log.info("user=%r action=%s detail=%r", username, action, detail)
