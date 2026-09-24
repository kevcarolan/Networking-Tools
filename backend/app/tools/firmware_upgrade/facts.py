"""Reads model, serial, software version, free flash and HA state from each
platform's show commands, and compares versions against the standards."""

import fnmatch
import re
from dataclasses import dataclass

# Commands run on each platform, in order. Only the first one must succeed;
# the rest add detail (flash space, failover state) and may be missing.
FACT_COMMANDS: dict[str, tuple[str, ...]] = {
    "cisco_ios": ("show version", "dir"),
    "cisco_nxos": ("show version", "dir bootflash:"),
    "cisco_asa": ("show version", "dir", "show failover"),
    "cisco_ftd": ("show version", "show failover"),
    "allied_awplus": ("show version", "show system"),
}

_CLI_ERRORS = ("% Invalid input", "% Incomplete command", "% Unknown command",
               "ERROR: % Invalid", "% Ambiguous command")


@dataclass
class Facts:
    version: str | None = None
    model: str | None = None
    serial: str | None = None
    image: str | None = None
    boot_mode: str | None = None
    ha_role: str | None = None
    flash_total: int | None = None
    flash_free: int | None = None


def _find(pattern: str, text: str, flags: int = re.M) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _first(*values: str | None) -> str | None:
    return next((v for v in values if v), None)


def _cli_error(text: str) -> bool:
    return any(marker in text[:300] for marker in _CLI_ERRORS)


def _dir_space(text: str, facts: Facts) -> None:
    # IOS / IOS-XE / ASA:  "11353194496 bytes total (8151625728 bytes free)"
    m = re.search(r"(\d+)\s+bytes total\s*\((\d+)\s+bytes free", text)
    if m:
        facts.flash_total, facts.flash_free = int(m.group(1)), int(m.group(2))
        return
    # NX-OS:  "   1234 bytes free" / "   5678 bytes total" on separate lines
    free, total = _find(r"^\s*(\d+)\s+bytes free", text), _find(r"^\s*(\d+)\s+bytes total", text)
    if free and total:
        facts.flash_total, facts.flash_free = int(total), int(free)


def _asa_failover(text: str) -> str | None:
    if re.search(r"^Failover Off", text, re.M):
        return "standalone"
    m = re.search(r"^\s*This host:\s*(\w+)\s*-\s*(.+?)\s*$", text, re.M)
    return f"{m.group(1)} / {m.group(2)}" if m else None


def _parse_ios(outputs: list[str]) -> Facts:
    ver = outputs[0]
    f = Facts()
    f.version = _first(_find(r"Cisco IOS XE Software, Version\s+(\S+)", ver),
                       _find(r"Cisco IOS Software.*?,\s*Version\s+([^\s,]+)", ver),
                       _find(r"\bVersion\s+([^\s,]+)", ver))
    f.model = _first(_find(r"^Model [Nn]umber\s*:\s*(\S+)", ver),
                     _find(r"^cisco\s+(\S+)\s+\(.*?\)\s+processor", ver, re.M | re.I))
    f.serial = _first(_find(r"^System [Ss]erial [Nn]umber\s*:\s*(\S+)", ver),
                      _find(r"Processor board ID\s+(\S+)", ver))
    f.image = _find(r'System image file is "([^"]+)"', ver)
    # IOS-XE switches list each member with its mode (INSTALL / BUNDLE).
    if re.search(r"^\*?\s*\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+INSTALL\s*$", ver, re.M) or \
            (f.image or "").endswith("packages.conf"):
        f.boot_mode = "install"
    elif re.search(r"^\*?\s*\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+BUNDLE\s*$", ver, re.M) or \
            (f.image or "").lower().endswith(".bin"):
        f.boot_mode = "bundle"
    return f


def _parse_nxos(outputs: list[str]) -> Facts:
    ver = outputs[0]
    f = Facts()
    f.version = _first(_find(r"NXOS:\s+version\s+(\S+)", ver),
                       _find(r"system:\s+version\s+(\S+)", ver))
    f.model = _find(r"cisco Nexus\S*\s+(\S+)\s+[Cc]hassis", ver)
    f.serial = _find(r"Processor [Bb]oard ID\s+(\S+)", ver)
    f.image = _first(_find(r"NXOS image file is:\s+(\S+)", ver),
                     _find(r"system image file is:\s+(\S+)", ver))
    return f


def _parse_asa(outputs: list[str]) -> Facts:
    ver = outputs[0]
    f = Facts()
    f.version = _find(r"Cisco Adaptive Security Appliance Software Version\s+(\S+)", ver)
    f.model = _find(r"^Hardware:\s+([^,\s]+)", ver)
    f.serial = _find(r"^Serial Number:\s+(\S+)", ver)
    f.image = _find(r'System image file is "([^"]+)"', ver)
    return f


def _parse_ftd(outputs: list[str]) -> Facts:
    ver = outputs[0]
    f = Facts()
    # "Model : Cisco Firepower 2110 Threat Defense (77) Version 7.2.5 (Build 208)"
    m = re.search(r"^Model\s*:\s*(.+?)\s+Version\s+(\S+)(?:\s+\(Build\s+(\d+)\))?", ver, re.M)
    if m:
        f.version = m.group(2)
        f.image = f"build {m.group(3)}" if m.group(3) else None
    f.model = _first(_find(r"^Hardware:\s+([^,\s]+)", ver),
                     re.sub(r"^Cisco\s+|\s*\(\d+\)$", "", m.group(1)) if m else None)
    f.serial = _find(r"^Serial Number:\s+(\S+)", ver)
    return f


def _parse_awplus(outputs: list[str]) -> Facts:
    ver = outputs[0]
    f = Facts()
    build = _find(r"^Build name\s*:\s*(\S+)", ver)
    f.image = build
    f.version = _first(_find(r"-(\d+\.\d+\.\d+[\w.-]*?)\.rel$", build or ""),
                       _find(r"AlliedWare Plus \(TM\)\s+(\S+)", ver),
                       _find(r"^Software version\s*:\s*(\S+)", ver))
    system = outputs[1] if len(outputs) > 1 else ""
    # "Base       414        x930-28GTX       B-0  A04563H182400035"
    m = re.search(r"^Base\s+\d+\s+(.*?)\s+(\S+)\s+(\S+)\s*$", system, re.M)
    if m:
        f.model, f.serial = m.group(1).split()[-1], m.group(3)
    else:
        f.model = _find(r"^(x\d+\S*|SBx\d+\S*|AT-\S+)-\d+\.\d+\.\d+", build or "")
    return f


_PARSERS = {
    "cisco_ios": _parse_ios,
    "cisco_nxos": _parse_nxos,
    "cisco_asa": _parse_asa,
    "cisco_ftd": _parse_ftd,
    "allied_awplus": _parse_awplus,
}


def parse_facts(platform: str, outputs: list[str]) -> Facts:
    """Turn command outputs (in FACT_COMMANDS order) into Facts."""
    commands = FACT_COMMANDS[platform]
    outputs = [o.replace("\r\n", "\n").replace("\r", "\n") for o in outputs]
    if not outputs or _cli_error(outputs[0]):
        raise ValueError("Device rejected 'show version'")
    facts = _PARSERS[platform](outputs)
    for cmd, out in zip(commands[1:], outputs[1:]):
        if _cli_error(out):
            continue
        if cmd.startswith("dir"):
            _dir_space(out, facts)
        elif cmd == "show failover":
            facts.ha_role = _asa_failover(out)
    if not facts.version:
        raise ValueError("Could not find the software version in 'show version'")
    return facts


# --- version comparison -----------------------------------------------------

def version_key(version: str) -> tuple:
    """Split a version into comparable parts, so that 17.09.04a == 17.9.4a,
    9.16(4)23 > 9.16(4) and 15.2(7)E8 > 15.2(7)E3."""
    parts = re.findall(r"\d+|[A-Za-z]+", version or "")
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts)


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def pattern_matches(pattern: str, model: str | None) -> bool:
    pattern = (pattern or "*").strip()
    if pattern == "*":
        return True
    return bool(model) and fnmatch.fnmatchcase(model.upper(), pattern.upper())


def best_standard(standards, platform: str, model: str | None):
    """The most specific standard for a device: platform must match, and the
    longest matching model pattern wins ("C9300-48*" beats "C9300-*" beats "*")."""
    candidates = [s for s in standards
                  if s.platform == platform and pattern_matches(s.model_pattern, model)]
    return max(candidates, key=lambda s: (s.model_pattern != "*", len(s.model_pattern)),
               default=None)


COMPLIANT, BEHIND, AHEAD, NO_STANDARD, UNKNOWN = (
    "compliant", "behind", "ahead", "no_standard", "unknown")


def compliance(current: str | None, target: str | None) -> str:
    if target is None:
        return NO_STANDARD
    if not current:
        return UNKNOWN
    c = compare_versions(current, target)
    return COMPLIANT if c == 0 else BEHIND if c < 0 else AHEAD
