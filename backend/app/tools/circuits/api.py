"""HTTP API for the master circuit list."""

import csv
import io
import json
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.auth import User, current_user, require_admin
from app.core.db import get_db, session_scope, utcnow
from app.core.models import Device
from app.tools.circuits.importer import (FIELDS, CircuitImportError, diff, match_switches,
                                         parse_workbook, row_key)
from app.tools.circuits.models import ACTIVE, PENDING, SUPERSEDED, Circuit, CircuitImport

router = APIRouter(prefix="/api/circuits", tags=["circuits"])

CSV_HEADERS = {
    "ip_partition": "IP Partition", "service": "Service", "eng_prefix": "Engineering prefix",
    "device_name": "Device Name", "device_type": "Device Type", "manufacturer": "Manufacturer",
    "mac": "Hardware Mac Address", "floor": "Floor Level", "room": "Room / Area",
    "ip": "IP Address", "mask": "Subnet Mask", "gateway": "Default Gateway", "vlan": "VLAN",
    "conn_type": "TYPE", "port": "PORT", "switch": "DEVICE", "drawing_number": "Drawing Number",
    "drawing_ref": "Device Drawing Reference",
}


def _active(db: Session) -> CircuitImport | None:
    return db.scalar(select(CircuitImport).where(CircuitImport.status == ACTIVE))


def _rows(db: Session, import_id: int) -> list[Circuit]:
    return db.scalars(select(Circuit).where(Circuit.import_id == import_id)
                      .order_by(Circuit.row_no)).all()


def _matches(db: Session, rows) -> dict[str, tuple[str, list[int]]]:
    names = dict(db.execute(select(Device.id, Device.name)).all())
    return match_switches(names, {r.switch_norm for r in rows if r.switch_norm})


def _row_out(c: Circuit, matches: dict, names: dict[int, str]) -> dict:
    status, ids = matches.get(c.switch_norm, ("none", []))
    out = {f: getattr(c, f) for f in FIELDS}
    out.update(id=c.id, row_no=c.row_no, port_norm=c.port_norm, warnings=c.warnings,
               match=status if c.switch_norm else "none",
               switch_device_id=ids[0] if status == "matched" else None,
               switch_device_name=names.get(ids[0]) if status == "matched" else None)
    return out


def _import_out(imp: CircuitImport) -> dict:
    return {"id": imp.id, "filename": imp.filename, "sheet": imp.sheet, "status": imp.status,
            "uploaded_by": imp.uploaded_by, "uploaded_at": imp.uploaded_at,
            "committed_by": imp.committed_by, "committed_at": imp.committed_at,
            "row_count": imp.row_count, "warning_count": imp.warning_count,
            "summary": json.loads(imp.summary or "{}")}


# --- the current list -------------------------------------------------------

@router.get("")
def list_circuits(db: Session = Depends(get_db), _: User = Depends(current_user)):
    imp = _active(db)
    if imp is None:
        return {"import": None, "circuits": []}
    rows = _rows(db, imp.id)
    matches = _matches(db, rows)
    names = dict(db.execute(select(Device.id, Device.name)).all())
    return {"import": _import_out(imp), "circuits": [_row_out(c, matches, names) for c in rows]}


@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db), _: User = Depends(current_user)):
    imp = _active(db)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([CSV_HEADERS[f] for f in FIELDS] + ["Matched NetOps device", "Warnings"])
    if imp:
        rows = _rows(db, imp.id)
        matches = _matches(db, rows)
        names = dict(db.execute(select(Device.id, Device.name)).all())
        for c in rows:
            r = _row_out(c, matches, names)
            w.writerow([r[f] for f in FIELDS] + [r["switch_device_name"] or r["match"],
                                                  r["warnings"]])
    return Response(out.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="circuit-list.csv"'})


@router.get("/for-device/{device_id}")
def for_device(device_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Circuits on one switch, grouped by service and VLAN (for upgrade planning)."""
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "Device not found")
    imp = _active(db)
    if imp is None:
        return {"device": device.name, "import": None, "count": 0, "groups": []}
    rows = _rows(db, imp.id)
    matches = _matches(db, rows)
    names = {device.id: device.name}
    mine = [c for c in rows if matches.get(c.switch_norm, ("", []))[1] == [device.id]]
    groups = defaultdict(list)
    for c in mine:
        groups[(c.service or "(no service)", c.vlan or "-")].append(_row_out(c, matches, names))
    return {"device": device.name, "import": _import_out(imp), "count": len(mine),
            "groups": [{"service": s, "vlan": v, "circuits": items}
                       for (s, v), items in sorted(groups.items())]}


# --- imports ----------------------------------------------------------------

@router.get("/imports")
def list_imports(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(CircuitImport).order_by(CircuitImport.id.desc()).limit(100))
    return [_import_out(i) for i in rows]


@router.get("/imports/{import_id}")
def get_import(import_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    imp = db.get(CircuitImport, import_id) or _not_found()
    return _import_out(imp)


@router.put("/imports/upload", status_code=201)
async def upload(request: Request, filename: str = Query(..., max_length=255),
                 user: User = Depends(require_admin)):
    """Upload a spreadsheet (raw .xlsx body). It is checked and kept as a pending
    import with a preview; nothing changes until it is committed."""
    limit = request.app.state.settings.circuits_max_upload_mb * 2**20
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, f"File is larger than {limit // 2**20} MB")
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(422, "Upload an Excel workbook (.xlsx)")
    try:
        parsed = await run_in_threadpool(parse_workbook, bytes(body))
    except CircuitImportError as exc:
        raise HTTPException(422, str(exc)) from None
    return await run_in_threadpool(_store_pending, parsed, filename, user.username)


def _store_pending(parsed, filename: str, username: str) -> dict:
    with session_scope() as db:
        # Only one pending import at a time: a new upload replaces the old preview.
        for old in db.scalars(select(CircuitImport).where(CircuitImport.status == PENDING)):
            db.delete(old)
        imp = CircuitImport(filename=filename[:255], sheet=parsed.sheet, uploaded_by=username,
                            row_count=len(parsed.rows),
                            warning_count=sum(bool(r.warnings) for r in parsed.rows))
        db.add(imp)
        db.flush()
        db.add_all(Circuit(import_id=imp.id, row_no=r.row_no, switch_norm=r.switch_norm,
                           port_norm=r.port_norm, mac_norm=r.mac_norm,
                           warnings="; ".join(r.warnings), **r.values) for r in parsed.rows)
        db.flush()

        new_rows = _rows(db, imp.id)
        active = _active(db)
        old_rows = _rows(db, active.id) if active else []
        matches = _matches(db, new_rows)
        unmatched = sorted({k for k, (s, _) in matches.items() if s == "unmatched"})
        ambiguous = sorted({k for k, (s, _) in matches.items() if s == "ambiguous"})
        values = lambda c: {f: getattr(c, f) for f in FIELDS}  # noqa: E731
        summary = {
            "header_row": parsed.header_row,
            "columns": parsed.columns,
            "missing_columns": [f for f in FIELDS if f not in parsed.columns],
            "switches": len(matches),
            "unmatched_switches": unmatched,
            "ambiguous_switches": ambiguous,
            "rows_unmatched": sum(1 for c in new_rows
                                  if matches.get(c.switch_norm, ("none",))[0] != "matched"),
            "warnings_sample": [{"row": c.row_no, "warnings": c.warnings}
                                for c in new_rows if c.warnings][:100],
            "diff": diff([(row_key(values(c), c.mac_norm), values(c)) for c in old_rows],
                         [(row_key(values(c), c.mac_norm), values(c)) for c in new_rows]),
            "replaces_import": active.id if active else None,
        }
        imp.summary = json.dumps(summary, default=str)
        audit(db, username, "circuits.import.preview",
              f"{filename}: {imp.row_count} rows, {imp.warning_count} with warnings")
        return _import_out(imp)


@router.post("/imports/{import_id}/commit")
def commit(import_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    imp = db.get(CircuitImport, import_id) or _not_found()
    if imp.status != PENDING:
        raise HTTPException(409, "Only a pending import can be committed")
    for old in db.scalars(select(CircuitImport).where(CircuitImport.status == ACTIVE)):
        old.status = SUPERSEDED
    imp.status, imp.committed_by, imp.committed_at = ACTIVE, user.username, utcnow()
    d = json.loads(imp.summary or "{}").get("diff", {})
    audit(db, user.username, "circuits.import.commit",
          f"{imp.filename}: {imp.row_count} rows (+{d.get('added', 0)} "
          f"~{d.get('changed', 0)} -{d.get('removed', 0)})")
    return _import_out(imp)


@router.delete("/imports/{import_id}", status_code=204)
def discard(import_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    imp = db.get(CircuitImport, import_id) or _not_found()
    if imp.status != PENDING:
        raise HTTPException(409, "Only a pending import can be discarded; "
                                 "earlier versions are kept as history")
    db.execute(delete(Circuit).where(Circuit.import_id == imp.id))
    db.delete(imp)
    audit(db, user.username, "circuits.import.discard", imp.filename)
    return Response(status_code=204)


def _not_found():
    raise HTTPException(404, "Import not found")
