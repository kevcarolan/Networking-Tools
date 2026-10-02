"""The upgrade job report: a self-contained page (prints well, no external
files) and a CSV of the checks. Every value is HTML-escaped."""

import csv
import io
from datetime import datetime
from html import escape

STATUS_TEXT = {
    "planned": "Planned", "pre_checking": "Pre-checking", "blocked": "Blocked by pre-checks",
    "ready": "Ready to start", "running": "Running", "post_checking": "Post-checking",
    "completed": "Completed", "completed_overrides": "Completed with overrides",
    "failed": "Failed post-checks", "cancelled": "Cancelled", "rolling_back": "Rolling back",
    "rolled_back": "Rolled back", "needs_attention": "Needs attention",
}


def _e(value) -> str:
    return escape("" if value is None else str(value))


def _t(value) -> str:
    """A stored UTC time to the second, e.g. '2026-10-02 08:01:53'."""
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _checks_table(rows: list[dict]) -> str:
    if not rows:
        return "<p class=muted>Not run.</p>"
    out = ["<table><tr><th>Check</th><th>Severity</th><th>Result</th><th>Value</th>"
           "<th>Detail</th><th>Override</th></tr>"]
    for r in rows:
        ov = (f"{_e(r['overridden_by'])}: {_e(r['override_reason'])}<br><small>"
              f"{_e(_t(r['overridden_at']))}</small>") if r["overridden_by"] else ""
        out.append(f"<tr class={_e(r['status'])}><td>{_e(r['label'])}</td><td>{_e(r['severity'])}</td>"
                   f"<td><b>{_e(r['status'].upper())}</b></td><td>{_e(r['value'])}</td>"
                   f"<td><pre>{_e(r['detail'])}</pre></td><td>{ov}</td></tr>")
    out.append("</table>")
    return "".join(out)


def _circuits(c: dict | None) -> str:
    if not c:
        return "<p class=muted>Not recorded.</p>"
    parts = [f"<p>{_e(c['count'])} circuit(s), from {_e(c['source'] or 'no circuit list')}, "
             f"recorded {_e(_t(c['created_at']))} UTC.</p>"]
    for g in c["groups"]:
        parts.append(f"<h4>{_e(g['service'])} - VLAN {_e(g['vlan'])} ({len(g['circuits'])})</h4>"
                     "<table><tr><th>Port</th><th>Device</th><th>Type</th><th>IP</th><th>MAC</th>"
                     "<th>Location</th><th>Drawing</th></tr>")
        for x in g["circuits"]:
            parts.append(f"<tr><td>{_e(x['port'])}</td><td>{_e(x['device_name'])}</td>"
                         f"<td>{_e(x['device_type'])}</td><td>{_e(x['ip'])}</td><td>{_e(x['mac'])}</td>"
                         f"<td>{_e(' / '.join(v for v in (x['floor'], x['room']) if v))}</td>"
                         f"<td>{_e(x['drawing_number'])}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def render_html(job: dict) -> str:
    d, img = job["device"], job["image"]
    rows = [("Device", f"{d['name']} ({d['address']}) - {d['platform_label']}, site {d['site'] or '-'}"),
            ("Upgrade", f"{job['from_version'] or '?'} → {job['target_version']}"),
            ("Image", f"{img['filename']} ({img['size'] // 2**20} MB), MD5 {img['md5']}"),
            ("Mode", "DRY RUN - nothing was changed on the device" if job["dry_run"] else "Live"),
            ("Status", STATUS_TEXT.get(job["status"], job["status"])),
            ("Change reference", job["change_ref"] or "-"),
            ("Planned window (UTC)", f"{_t(job['planned_start'])} to {_t(job['planned_end'])}"
                                     if job["planned_start"] else "not set"),
            ("Created (UTC)", f"{_t(job['created_at'])} by {job['created_by']}"),
            ("Started (UTC)", f"{_t(job['started_at'])} by {job['started_by']}"
                              if job["started_at"] else "not started"),
            ("Finished (UTC)", _t(job["finished_at"])),
            ("Config backup before / after", f"{(job['pre_backup_commit'] or '-')[:12]} / "
                                             f"{(job['post_backup_commit'] or '-')[:12]}"),
            ("Upgrade account", job["upgrade_credential"] or "-"),
            ("Notes", job["notes"] or "-"), ("Outcome note", job["outcome_note"] or "-")]
    summary = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)
    events = "".join(f"<tr class={_e(e['level'])}><td>{_e(_t(e['time']))}</td><td>{_e(e['step'])}</td>"
                     f"<td>{_e(e['message'])}</td></tr>" for e in job["events"])
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<title>Upgrade job {job['id']} - {_e(d['name'])}</title>
<style>
 body {{ font: 13px system-ui, sans-serif; margin: 24px; color: #1c2330; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; border-bottom: 1px solid #ccc; }}
 h4 {{ margin: 14px 0 4px; }}
 table {{ border-collapse: collapse; width: 100%; margin: 6px 0; }}
 th, td {{ border: 1px solid #d5d9e0; padding: 4px 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f2f4f7; }} pre {{ margin: 0; white-space: pre-wrap; font-size: 12px; }}
 tr.fail td, tr.error td, tr.warn td {{ background: #fdecea; }} tr.skip td {{ color: #777; }}
 tr.action td {{ font-weight: 600; }} .muted {{ color: #667; }}
 .banner {{ padding: 8px 12px; border-radius: 6px; background: #fff4e0; border: 1px solid #f0c060; }}
 @media print {{ body {{ margin: 0; }} }}
</style></head><body>
<h1>Upgrade job #{job['id']}: {_e(d['name'])}</h1>
{'<p class=banner>DRY RUN: the upgrade steps were recorded, not carried out.</p>' if job['dry_run'] else ''}
<table>{summary}</table>
<h2>Pre-checks (run {job['pre_run']})</h2>{_checks_table(job['checks']['pre'])}
<h2>Post-checks (run {job['post_run']})</h2>{_checks_table(job['checks']['post'])}
<h2>Affected circuits at start</h2>{_circuits(job['circuits'].get('started') or job['circuits'].get('planned'))}
<h2>Log</h2><table><tr><th>Time (UTC)</th><th>Step</th><th>Event</th></tr>{events}</table>
<p class=muted>Generated by NetOps Tools.</p></body></html>"""


def render_csv(job: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["job", "device", "phase", "run", "check", "severity", "result", "value", "detail",
                "overridden_by", "override_reason"])
    for phase in ("pre", "post"):
        for r in job["checks"][phase]:
            w.writerow([job["id"], job["device"]["name"], phase, r["run_no"], r["label"],
                        r["severity"], r["status"], r["value"], r["detail"],
                        r["overridden_by"] or "", r["override_reason"] or ""])
    return out.getvalue()
