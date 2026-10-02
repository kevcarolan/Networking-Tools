"""Pre-checks, post-checks and the before/after device snapshot for upgrade jobs.

Everything here is pure parsing and rules - no SSH, no database - so it can be
tested with sample output. jobs.py runs the commands and stores the results.

Severity: a failed BLOCKER stops the job until it is fixed (re-check) or
overridden with a reason; a failed WARNING is shown but doesn't stop it; INFO is
for the record. A check that can't run on a device (command not supported) is SKIP.
"""

import re
from dataclasses import asdict, dataclass

from app.tools.firmware_upgrade.facts import (FACT_COMMANDS, compare_versions, parse_facts,
                                              pattern_matches)

BLOCKER, WARNING, INFO = "blocker", "warning", "info"
PASS, FAIL, SKIP, ERROR = "pass", "fail", "skip", "error"

_CLI_ERRORS = ("% Invalid input", "% Incomplete command", "% Unknown command",
               "ERROR: % Invalid", "% Ambiguous command", "Invalid command", "% Error")


@dataclass
class CheckResult:
    check_id: str
    label: str
    severity: str
    status: str
    value: str = ""
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCKER and self.status in (FAIL, ERROR)

    def as_dict(self) -> dict:
        return asdict(self)


def _unsupported(out: str | None) -> bool:
    return out is None or any(m in out[:400] for m in _CLI_ERRORS)


def _skip(check_id, label, severity, cmd) -> CheckResult:
    return CheckResult(check_id, label, severity, SKIP, "",
                       f"'{cmd}' is not supported on this device")


# --- commands per platform ----------------------------------------------------

IOS_HEALTH = {
    "version": "show version",
    "dir": "dir",
    "alarms": "show facility-alarm status",
    "env": "show environment all",
    "cpu": "show processes cpu | include CPU utilization",
    "mem": "show processes memory | include Processor Pool",
    "install": "show install summary",
    "unsaved": "show archive config differences",
    "stack": "show switch",
}
IOS_SNAPSHOT = {
    "interfaces": "show ip interface brief",
    "cdp": "show cdp neighbors detail",
    "lldp": "show lldp neighbors detail",
    "etherchannel": "show etherchannel summary",
    "mac": "show mac address-table count",
}
ASA_HEALTH = {
    "version": "show version",
    "dir": "dir",
    "failover": "show failover",
    "cpu": "show cpu usage",
    "mem": "show memory",
}
ASA_SNAPSHOT = {"interfaces": "show interface ip brief"}
GENERIC_HEALTH = {"version": "show version"}

COMMANDS: dict[str, tuple[dict, dict]] = {
    "cisco_ios": (IOS_HEALTH, IOS_SNAPSHOT),
    "cisco_asa": (ASA_HEALTH, ASA_SNAPSHOT),
    "cisco_ftd": ({"version": "show version", "failover": "show failover"}, {}),
    "cisco_nxos": ({"version": "show version", "dir": "dir bootflash:"}, {}),
    "allied_awplus": ({"version": "show version", "system": "show system"}, {}),
}
FULL_CHECKS = {"cisco_ios"}  # platforms with the full health-check set


def commands_for(platform: str) -> dict[str, str]:
    health, snap = COMMANDS.get(platform, (GENERIC_HEALTH, {}))
    return {**health, **{f"snap:{k}": v for k, v in snap.items()}}


def facts_outputs(platform: str, outputs: dict[str, str]) -> list[str]:
    """The outputs in the order facts.parse_facts expects (FACT_COMMANDS)."""
    by_command = {cmd: outputs.get(key) for key, cmd in commands_for(platform).items()}
    return [by_command.get(cmd) or "" for cmd in FACT_COMMANDS.get(platform, ("show version",))]


# --- parsers ------------------------------------------------------------------

def parse_alarms(out: str) -> dict:
    totals = {k.lower(): int(v) for k, v in
              re.findall(r"\b(Critical|Major|Minor)\s*:\s*(\d+)", out, re.I)}
    rows = []
    for line in out.splitlines():
        m = re.search(r"\b(CRITICAL|MAJOR|MINOR)\b\s+(.+?)\s*$", line)
        if m and not re.search(r"Critical\s*:", line, re.I):
            rows.append(f"{m.group(1)}: {m.group(2)}")
    for sev in ("critical", "major", "minor"):
        totals.setdefault(sev, sum(r.lower().startswith(sev) for r in rows))
    return {**totals, "rows": rows}


_BAD_ENV = re.compile(r"\b(FAIL(?:ED|URE)?|FAULTY|BAD|NOT\s+OK|RED|CRITICAL|SHUTDOWN)\b", re.I)


def parse_environment(out: str) -> list[str]:
    """Lines that report a fault (fans, power supplies, temperature)."""
    return [ln.strip() for ln in out.splitlines()
            if _BAD_ENV.search(ln) and not re.search(r"Range|threshold|Status\s+Sys", ln, re.I)]


def parse_cpu(out: str) -> int | None:
    m = (re.search(r"five minutes:\s*(\d+)%", out)
         or re.search(r"5 minutes:\s*(\d+)%", out))
    return int(m.group(1)) if m else None


def parse_memory(out: str) -> int | None:
    m = re.search(r"Processor Pool Total:\s*(\d+)\s+Used:\s*(\d+)", out)
    if m and int(m.group(1)):
        return round(100 * int(m.group(2)) / int(m.group(1)))
    m = re.search(r"Used memory:.*\(\s*(\d+)%\)", out)  # ASA
    return int(m.group(1)) if m else None


def parse_install_summary(out: str) -> dict:
    states = re.findall(r"^\s*\w+\s+([IUCD])\s+(\S+)\s*$", out, re.M)
    abort = re.search(r"Auto abort timer:\s*(\S+)", out)
    return {
        "pending": [f"{st} {name}" for st, name in states if st in ("U", "D")],
        "inactive": [name for st, name in states if st == "I"],
        "committed": [name for st, name in states if st == "C"],
        "abort_timer": abort.group(1) if abort else "",
    }


def parse_unsaved(out: str) -> int | None:
    """Number of changed lines between running and startup config (None = unknown)."""
    if re.search(r"No changes were found", out, re.I):
        return 0
    lines = [ln for ln in out.splitlines() if re.match(r"^\s*[+-]\S", ln)]
    return len(lines) if lines else None


def parse_stack(out: str) -> dict[str, str]:
    """Switch number -> state (e.g. Ready, Removed, Provisioned)."""
    rows = re.findall(r"^\*?\s*(\d+)\s+(?:Active|Standby|Member)\s+\S+\s+\d+\s+\S+\s+(\S+)",
                      out, re.M | re.I)
    return {num: state for num, state in rows}


def parse_failover(out: str) -> dict:
    if re.search(r"^\s*Failover Off", out, re.M):
        return {"enabled": False}
    this = re.search(r"This host:\s*(\w+)\s*-\s*(.+?)\s*$", out, re.M)
    other = re.search(r"Other host:\s*(\w+)\s*-\s*(.+?)\s*$", out, re.M)
    return {"enabled": True, "this": this.group(2) if this else "",
            "other": other.group(2) if other else "",
            "this_unit": this.group(1) if this else "", "other_unit": other.group(1) if other else ""}


def parse_ip_brief(out: str) -> dict[str, str]:
    """Interface -> 'up' / 'down' / 'admin-down' (IOS 'show ip interface brief', ASA)."""
    result = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+\S+\s+(?:YES|NO)\s+\S+\s+(administratively down|up|down)\s+(up|down)",
                     line.strip())
        if m:
            name, status, proto = m.groups()
            result[name] = "admin-down" if status.startswith("admin") else (
                "up" if status == "up" and proto == "up" else "down")
    return result


def parse_cdp_detail(out: str) -> list[str]:
    """'local-port > neighbour' strings."""
    pairs = []
    for block in re.split(r"^-{5,}\s*$", out, flags=re.M):
        dev = re.search(r"Device ID:\s*(\S+)", block)
        intf = re.search(r"Interface:\s*([^,\s]+)", block)
        if dev and intf:
            pairs.append(f"{intf.group(1)} > {dev.group(1).split('.')[0]}")
    return sorted(set(pairs))


def parse_lldp_detail(out: str) -> list[str]:
    pairs = []
    for block in re.split(r"^-{5,}\s*$", out, flags=re.M):
        name = re.search(r"System Name:\s*(\S+)", block)
        intf = re.search(r"Local Intf:\s*(\S+)", block)
        if name and intf:
            pairs.append(f"{intf.group(1)} > {name.group(1).split('.')[0]}")
    return sorted(set(pairs))


def parse_etherchannel(out: str) -> dict[str, list[str]]:
    """Port-channel -> members currently bundled (flag P)."""
    result = {}
    for line in out.splitlines():
        m = re.match(r"^\s*\d+\s+(Po\d+)\(\w+\)\s+\S+\s*(.*)$", line)
        if m:
            result[m.group(1)] = sorted(re.findall(r"(\S+?)\(P\)", m.group(2)))
    return result


def parse_mac_count(out: str) -> int | None:
    totals = re.findall(r"Total Mac Addresses for this criterion:\s*(\d+)", out, re.I)
    if totals:
        return int(totals[-1])
    m = re.search(r"Total\s+Mac\s+Address(?:es)?.*?:\s*(\d+)", out, re.I)
    return int(m.group(1)) if m else None


# --- the snapshot ----------------------------------------------------------------

def snapshot(platform: str, outputs: dict[str, str]) -> dict:
    """Device state used for the before/after comparison."""
    snap: dict = {}
    get = outputs.get
    if not _unsupported(get("version")):
        try:
            f = parse_facts(platform, facts_outputs(platform, outputs))
            snap["version"], snap["model"] = f.version, f.model
        except ValueError:
            pass
        up = re.search(r"uptime is (.+)$", get("version") or "", re.M)
        snap["uptime"] = up.group(1).strip() if up else ""
        snap["ios_xe"] = bool(re.search(r"IOS[ -]XE", get("version") or ""))
    if not _unsupported(get("snap:interfaces")):
        snap["interfaces"] = parse_ip_brief(get("snap:interfaces"))
    if not _unsupported(get("snap:cdp")):
        snap["cdp"] = parse_cdp_detail(get("snap:cdp"))
    if not _unsupported(get("snap:lldp")):
        snap["lldp"] = parse_lldp_detail(get("snap:lldp"))
    if not _unsupported(get("snap:etherchannel")):
        snap["etherchannel"] = parse_etherchannel(get("snap:etherchannel"))
    if not _unsupported(get("snap:mac")):
        snap["mac_count"] = parse_mac_count(get("snap:mac"))
    if not _unsupported(get("stack")):
        snap["stack"] = parse_stack(get("stack"))
    if not _unsupported(get("failover")):
        snap["failover"] = parse_failover(get("failover"))
    return snap


# --- health checks (run before and after) ---------------------------------------

@dataclass
class Thresholds:
    cpu_warn: int = 80
    mem_warn: int = 85


def health_checks(platform: str, outputs: dict[str, str], th: Thresholds) -> list[CheckResult]:
    results: list[CheckResult] = []
    get = outputs.get
    cmds = commands_for(platform)

    if "alarms" in cmds:
        out = get("alarms")
        if _unsupported(out):
            results.append(_skip("alarms", "Active alarms", BLOCKER, cmds["alarms"]))
        else:
            a = parse_alarms(out)
            serious = [r for r in a["rows"] if not r.startswith("MINOR")]
            bad = a["critical"] + a["major"]
            results.append(CheckResult(
                "alarms", "No critical or major alarms", BLOCKER, FAIL if bad else PASS,
                f"critical {a['critical']}, major {a['major']}", "\n".join(serious)))
            results.append(CheckResult(
                "alarms_minor", "No minor alarms", WARNING, FAIL if a["minor"] else PASS,
                f"minor {a['minor']}", "\n".join(r for r in a["rows"] if r.startswith("MINOR"))))

    if "env" in cmds:
        out = get("env")
        if _unsupported(out):
            results.append(_skip("environment", "Fans, power and temperature OK", BLOCKER,
                                 cmds["env"]))
        else:
            faults = parse_environment(out)
            results.append(CheckResult("environment", "Fans, power and temperature OK", BLOCKER,
                                       FAIL if faults else PASS,
                                       f"{len(faults)} fault(s)" if faults else "all OK",
                                       "\n".join(faults)))

    if "cpu" in cmds:
        out = get("cpu")
        cpu = None if _unsupported(out) else parse_cpu(out)
        results.append(CheckResult("cpu", f"CPU (5 min) below {th.cpu_warn}%", WARNING,
                                   SKIP if cpu is None else (FAIL if cpu > th.cpu_warn else PASS),
                                   "" if cpu is None else f"{cpu}%"))
    if "mem" in cmds:
        out = get("mem")
        mem = None if _unsupported(out) else parse_memory(out)
        results.append(CheckResult("memory", f"Memory use below {th.mem_warn}%", WARNING,
                                   SKIP if mem is None else (FAIL if mem > th.mem_warn else PASS),
                                   "" if mem is None else f"{mem}%"))

    if "install" in cmds:
        out = get("install")
        if _unsupported(out):
            results.append(_skip("install_state", "No install operation pending", BLOCKER,
                                 cmds["install"]))
        else:
            ins = parse_install_summary(out)
            pending = ins["pending"] or ([f"auto-abort timer {ins['abort_timer']}"]
                                         if ins["abort_timer"] not in ("", "inactive") else [])
            results.append(CheckResult("install_state", "No install operation pending", BLOCKER,
                                       FAIL if pending else PASS,
                                       "pending" if pending else "nothing pending",
                                       "\n".join(pending)))
            if ins["inactive"]:
                results.append(CheckResult("install_inactive", "Old inactive images on flash",
                                           INFO, PASS, f"{len(ins['inactive'])} inactive",
                                           "\n".join(ins["inactive"])))

    if "stack" in cmds:
        out = get("stack")
        if _unsupported(out):
            results.append(_skip("stack", "All stack members Ready", BLOCKER, cmds["stack"]))
        else:
            stack = parse_stack(out)
            bad = {n: s for n, s in stack.items() if s.lower() != "ready"}
            results.append(CheckResult("stack", "All stack members Ready", BLOCKER,
                                       FAIL if bad else PASS,
                                       f"{len(stack)} member(s)" if stack else "standalone",
                                       "\n".join(f"switch {n}: {s}" for n, s in bad.items())))

    if "failover" in cmds:
        out = get("failover")
        if _unsupported(out):
            results.append(_skip("failover", "Failover pair healthy", BLOCKER, cmds["failover"]))
        else:
            fo = parse_failover(out)
            if not fo["enabled"]:
                results.append(CheckResult("failover", "Failover pair healthy", BLOCKER, PASS,
                                           "failover off (standalone)"))
            else:
                ok = {"active", "standby ready"}
                healthy = fo["this"].lower() in ok and fo["other"].lower() in ok
                results.append(CheckResult(
                    "failover", "Failover pair healthy", BLOCKER, PASS if healthy else FAIL,
                    f"this: {fo['this_unit']} {fo['this']}; other: {fo['other_unit']} {fo['other']}"))

    if platform not in FULL_CHECKS and platform != "cisco_asa":
        results.append(CheckResult(
            "platform_checks", "Platform-specific health checks", INFO, SKIP, "",
            "Alarm/environment/stack checks for this platform arrive with its upgrade driver"))
    return results


# --- pre-check only ----------------------------------------------------------------

@dataclass
class ImageFacts:
    platform: str
    version: str
    model_pattern: str
    size: int
    md5_ok: bool | None  # file on disk re-hashed and matches the library (None = missing)
    filename: str


def pre_checks(platform: str, outputs: dict[str, str], image: ImageFacts, th: Thresholds,
               flash_factor: float, flash_factor_install: float) -> tuple[list[CheckResult], dict]:
    """Checks that need the device output plus the target image. Returns (results, facts)."""
    results = []
    facts: dict = {}
    get = outputs.get
    try:
        f = parse_facts(platform, facts_outputs(platform, outputs))
        facts = {"version": f.version, "model": f.model, "boot_mode": f.boot_mode,
                 "flash_free": f.flash_free, "flash_total": f.flash_total, "image": f.image,
                 "ha_role": f.ha_role}
    except ValueError as exc:
        results.append(CheckResult("version_read", "Read software version", BLOCKER, ERROR, "",
                                   str(exc)))

    if facts:
        results.append(CheckResult(
            "image_model", "Image suits this model", BLOCKER,
            PASS if pattern_matches(image.model_pattern, facts["model"]) else FAIL,
            f"model {facts['model'] or '?'}, image for {image.model_pattern}"))
        same = facts["version"] and compare_versions(facts["version"], image.version) == 0
        results.append(CheckResult(
            "version_differs", "Device isn't already on the target version", BLOCKER,
            FAIL if same else PASS, f"{facts['version']} → {image.version}"))
        is_xe = "IOS XE" in (get("version") or "") or "IOS-XE" in (get("version") or "")
        install = platform == "cisco_ios" and is_xe and facts["boot_mode"] == "install"
        factor = flash_factor_install if install else flash_factor
        need = int(image.size * factor)
        free = facts["flash_free"]
        results.append(CheckResult(
            "flash_space", "Enough free flash for the image", BLOCKER,
            SKIP if free is None else (PASS if free >= need else FAIL),
            "" if free is None else f"{free // 2**20} MB free, {need // 2**20} MB needed",
            f"image {image.size // 2**20} MB × {factor}"))
        if platform == "cisco_ios":
            out = get("unsaved")
            n = None if _unsupported(out) else parse_unsaved(out)
            results.append(CheckResult(
                "unsaved_config", "No unsaved configuration changes", BLOCKER,
                SKIP if n is None else (FAIL if n else PASS),
                "" if n is None else ("saved" if n == 0 else f"{n} changed line(s)"),
                "Save with 'write memory' (or check why the running config differs)" if n else ""))

    return image_checks(platform, image) + results + health_checks(platform, outputs, th), facts


def image_checks(platform: str, image: ImageFacts) -> list[CheckResult]:
    """Checks on the image itself - they don't need the device."""
    return [
        CheckResult("image_platform", "Image is for this platform", BLOCKER,
                    PASS if image.platform == platform else FAIL, image.platform),
        CheckResult("image_md5", "Image file on the server matches its MD5", BLOCKER,
                    FAIL if not image.md5_ok else PASS, image.filename,
                    "" if image.md5_ok else
                    ("image file is missing from the server" if image.md5_ok is None
                     else "the file on disk no longer matches the MD5 recorded when it was uploaded")),
    ]


# --- post-check: compare with the snapshot taken before ---------------------------

def compare_snapshots(pre: dict, post: dict, target_version: str) -> list[CheckResult]:
    results = []
    if "version" in post:
        ok = compare_versions(post.get("version") or "", target_version) == 0
        results.append(CheckResult("post_version", "Device runs the target version", BLOCKER,
                                   PASS if ok else FAIL,
                                   f"{post.get('version')} (target {target_version})"))
    if "interfaces" in pre and "interfaces" in post:
        lost = sorted(i for i, s in pre["interfaces"].items()
                      if s == "up" and post["interfaces"].get(i) != "up")
        results.append(CheckResult(
            "post_interfaces", "Interfaces that were up are still up", BLOCKER,
            FAIL if lost else PASS,
            f"{len(lost)} down" if lost else
            f"{sum(s == 'up' for s in post['interfaces'].values())} up", "\n".join(lost)))
    for key, label in (("cdp", "CDP neighbours"), ("lldp", "LLDP neighbours")):
        if pre.get(key) and key in post:
            gone = sorted(set(pre[key]) - set(post[key]))
            results.append(CheckResult(
                f"post_{key}", f"All {label} still present", BLOCKER, FAIL if gone else PASS,
                f"{len(gone)} missing" if gone else f"{len(post[key])} present", "\n".join(gone)))
    if pre.get("etherchannel") and "etherchannel" in post:
        missing = [f"{po}: {m}" for po, members in pre["etherchannel"].items()
                   for m in members if m not in post["etherchannel"].get(po, [])]
        results.append(CheckResult("post_etherchannel", "Port-channel members still bundled",
                                   BLOCKER, FAIL if missing else PASS,
                                   f"{len(missing)} not bundled" if missing else "all bundled",
                                   "\n".join(missing)))
    if pre.get("mac_count") and post.get("mac_count") is not None:
        before, after = pre["mac_count"], post["mac_count"]
        drop = (before - after) / before if before else 0
        results.append(CheckResult("post_mac", "MAC address count similar to before", WARNING,
                                   FAIL if drop > 0.2 else PASS, f"{before} → {after}"))
    if pre.get("stack") and "stack" in post:
        missing = sorted(set(pre["stack"]) - set(post["stack"]))
        results.append(CheckResult("post_stack", "All stack members back", BLOCKER,
                                   FAIL if missing else PASS,
                                   f"{len(post['stack'])} of {len(pre['stack'])}",
                                   "\n".join(f"switch {n} missing" for n in missing)))
    return results
