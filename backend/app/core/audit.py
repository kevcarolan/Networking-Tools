from sqlalchemy.orm import Session

from app.core.models import AuditEvent


def audit(db: Session, username: str, action: str, detail: str = "") -> None:
    db.add(AuditEvent(username=username, action=action, detail=detail))
