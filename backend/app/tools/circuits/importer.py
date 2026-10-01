"""Reads the master circuit list spreadsheet (.xlsx), and the rules for
linking a row to a NetOps device and interface.

The header is found by its column names (not by position), so column order and
the coloured band rows above it don't matter."""

import io
import ipaddress
import re
from dataclasses import dataclass, field

FIELDS = (
    "ip_partition", "service", "eng_prefix", "device_name", "device_type", "manufacturer",
    "mac", "floor", "room", "ip", "mask", "gateway", "vlan", "conn_type", "port", "switch",
    "drawing_number", "drawing_ref",
)

# Header text (normalised: lower case, letters/digits only, single spaces) -> field
_HEADERS = {
    "ip partition": "ip_partition", "partition": "ip_partition",
    "service": "service",
    "engineering prefix": "eng_prefix", "eng prefix": "eng_prefix",
    "device name": "device_name",
    "device type": "device_type",
    "manufacturer": "manufacturer", "manufacture": "manufacturer",
    "hardware mac address": "mac", "mac address": "mac", "mac": "mac",
    "floor level": "floor", "floor": "floor",
    "room area": "room", "room": "room",
    "ip address": "ip", "ip": "ip",
    "subnet mask": "mask", "mask": "mask",
    "default gateway": "gateway", "gateway": "gateway",
    "vlan": "vlan", "vlan id": "vlan",
    "type": "conn_type",
    "port": "port", "switch port": "port",
    "device": "switch", "switch": "switch",
    "drawing number": "drawing_number",
    "device drawing reference": "drawing_ref", "drawing reference": "drawing_ref",
}
REQUIRED = ("switch", "port")
MIN_HEADER_MATCHES = 6


class CircuitImportError(Exception):
    pass


@dataclass
class ParsedRow:
    row_no: int
    values: dict[str, str]
    switch_norm: str
    port_norm: str
    mac_norm: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedSheet:
    sheet: str
    header_row: int
    columns: dict[str, str]  # field -> header text found
    rows: list[ParsedRow]


def _norm_header(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


# --- normalisation used for matching ----------------------------------------

_PORT_PREFIXES = (  # longest first
    ("twentyfivegige", "twe"), ("hundredgigabitethernet", "hu"), ("hundredgige", "hu"),
    ("fortygigabitethernet", "fo"), ("tengigabitethernet", "te"), ("twogigabitethernet", "tw"),
    ("fivegigabitethernet", "fi"), ("gigabitethernet", "gi"), ("fastethernet", "fa"),
    ("port-channel", "po"), ("ethernet", "eth"), ("twe", "twe"), ("gig", "gi"),
    ("ten", "te"), ("eth", "eth"), ("gi", "gi"), ("te", "te"), ("tw", "tw"), ("fi", "fi"),
    ("fa", "fa"), ("fo", "fo"), ("hu", "hu"), ("po", "po"), ("port", "port"), ("e", "eth"),
)


def normalize_port(port: str) -> str:
    """'Gi2/0/13', 'GigabitEthernet2/0/13' and 'gi 2/0/13' -> 'gi2/0/13';
    AlliedWare Plus 'port1.0.1' -> 'port1.0.1'."""
    p = re.sub(r"\s+", "", (port or "").lower())
    m = re.match(r"^([a-z-]+)(\d.*)$", p)
    if not m:
        return p
    name, rest = m.groups()
    for long, short in _PORT_PREFIXES:
        if name == long:
            return short + rest
    return p


def normalize_switch(name: str) -> str:
    return (name or "").strip().lower()


def normalize_mac(mac: str) -> str:
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()
    if len(hexdigits) != 12:
        return ""
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))


def switch_matches(device_name: str, key: str) -> bool:
    """Does a NetOps device name match a circuit-list switch name?

    'GH-AS01' matches 'GH-AS01', 'gh-as01.corp.local' and 'DUB01-GH-AS01'."""
    d, k = device_name.lower(), key.lower()
    if not k:
        return False
    return d == k or d.split(".")[0] == k or d.endswith("-" + k) or d.endswith("_" + k)


def match_switches(device_names: dict[int, str], keys: set[str]) -> dict[str, tuple[str, list[int]]]:
    """For each switch key: ('matched', [id]) / ('ambiguous', [ids]) / ('unmatched', [])."""
    result = {}
    for key in keys:
        ids = [i for i, name in device_names.items() if switch_matches(name, key)]
        exact = [i for i in ids if device_names[i].lower().split(".")[0] == key]
        if exact:
            ids = exact
        result[key] = ("matched", ids) if len(ids) == 1 else (
            ("ambiguous", ids) if ids else ("unmatched", []))
    return result


# --- validation -------------------------------------------------------------

def _validate(v: dict[str, str]) -> list[str]:
    w = []
    if not v["switch"] or not v["port"]:
        w.append("no switch/port (NETWORK PORT DEVICE and PORT)")
    for key, label in (("ip", "IP address"), ("gateway", "default gateway")):
        if v[key]:
            try:
                ipaddress.ip_address(v[key])
            except ValueError:
                w.append(f"{label} '{v[key]}' is not a valid IP")
    if v["mask"]:
        try:
            ipaddress.ip_network(f"0.0.0.0/{v['mask']}")
        except ValueError:
            w.append(f"subnet mask '{v['mask']}' is not valid")
    if v["vlan"] and not (v["vlan"].isdigit() and 1 <= int(v["vlan"]) <= 4094):
        w.append(f"VLAN '{v['vlan']}' is not 1-4094")
    if v["mac"] and not normalize_mac(v["mac"]):
        w.append(f"MAC '{v['mac']}' is not a valid MAC address")
    return w


# --- reading the workbook ---------------------------------------------------

def _find_header(rows: list[tuple]) -> tuple[int, dict[int, str], dict[str, str]] | None:
    for idx, row in enumerate(rows):
        mapping, found = {}, {}
        for col, cell in enumerate(row):
            fld = _HEADERS.get(_norm_header(cell))
            if fld and fld not in found:
                mapping[col], found[fld] = fld, _cell(cell)
        if len(found) >= MIN_HEADER_MATCHES:
            return idx, mapping, found
    return None


def parse_workbook(data: bytes, max_rows: int = 50000) -> ParsedSheet:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - zip/xml errors of a non-xlsx file
        raise CircuitImportError(f"Not a readable .xlsx workbook ({type(exc).__name__})") from None
    try:
        for ws in wb.worksheets:
            head = [tuple(r) for r in ws.iter_rows(min_row=1, max_row=15, values_only=True)]
            found = _find_header(head)
            if not found:
                continue
            header_idx, mapping, columns = found
            missing = [f for f in REQUIRED if f not in columns]
            if missing:
                raise CircuitImportError(
                    f"Sheet '{ws.title}': columns {', '.join(missing)} not found "
                    "(expected NETWORK PORT 'DEVICE' and 'PORT')")
            rows = []
            start = header_idx + 2  # 1-based row number of the first data row
            for row_no, raw in enumerate(ws.iter_rows(min_row=start, values_only=True), start):
                values = {f: "" for f in FIELDS}
                for col, fld in mapping.items():
                    if col < len(raw):
                        values[fld] = _cell(raw[col])
                if not any(values.values()):
                    continue
                if len(rows) >= max_rows:
                    raise CircuitImportError(f"More than {max_rows} rows - is this the right sheet?")
                rows.append(ParsedRow(
                    row_no=row_no, values=values, switch_norm=normalize_switch(values["switch"]),
                    port_norm=normalize_port(values["port"]), mac_norm=normalize_mac(values["mac"]),
                    warnings=_validate(values)))
            return ParsedSheet(sheet=ws.title, header_row=header_idx + 1, columns=columns,
                               rows=rows)
    finally:
        wb.close()
    raise CircuitImportError("No sheet has the circuit list header (e.g. 'Device Name', "
                       "'IP Address', 'VLAN', 'PORT', 'DEVICE') in its first 15 rows")


# --- comparing two versions of the list -------------------------------------

def row_key(values: dict[str, str], mac_norm: str) -> str:
    """Identity of a row across imports: its MAC, else its device name, else its port."""
    if mac_norm:
        return "mac:" + mac_norm
    if values.get("device_name"):
        return "name:" + values["device_name"].lower()
    return f"port:{normalize_switch(values.get('switch', ''))}/{normalize_port(values.get('port', ''))}"


def diff(old: list[tuple[str, dict]], new: list[tuple[str, dict]], sample: int = 50) -> dict:
    """Compare (key, values) lists. Duplicate keys are compared in order."""
    def index(rows):
        out, seen = {}, {}
        for key, values in rows:
            n = seen.get(key, 0)
            seen[key] = n + 1
            out[f"{key}#{n}"] = values
        return out

    o, n = index(old), index(new)
    added = [k for k in n if k not in o]
    removed = [k for k in o if k not in n]
    changed = []
    for k in n.keys() & o.keys():
        fields = [f for f in FIELDS if (o[k].get(f) or "") != (n[k].get(f) or "")]
        if fields:
            changed.append((k, fields))

    def label(values):
        return values.get("device_name") or values.get("mac") or \
            f"{values.get('switch')} {values.get('port')}"

    return {
        "added": len(added), "removed": len(removed), "changed": len(changed),
        "unchanged": len(n) - len(added) - len(changed),
        "added_sample": [label(n[k]) for k in added[:sample]],
        "removed_sample": [label(o[k]) for k in removed[:sample]],
        "changed_sample": [{"row": label(n[k]), "fields": f,
                            "before": {x: o[k].get(x, "") for x in f},
                            "after": {x: n[k].get(x, "") for x in f}}
                           for k, f in sorted(changed)[:sample]],
    }
