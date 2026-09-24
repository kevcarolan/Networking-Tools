"""Supported device platforms. Adding a vendor = adding an entry here."""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    netmiko_type: str
    backup_commands: tuple[str, ...] = ("show running-config",)
    needs_enable: bool = True
    # Lines that change on every run (timestamps, checksums) and would make
    # every backup look like a config change.
    volatile_lines: tuple[str, ...] = field(default_factory=tuple)

    def volatile_regex(self) -> re.Pattern | None:
        if not self.volatile_lines:
            return None
        return re.compile("|".join(f"(?:{p})" for p in self.volatile_lines))


_IOS_VOLATILE = (
    r"^Building configuration",
    r"^Current configuration\s*:",
    r"^! Last configuration change at",
    r"^! NVRAM config last updated",
    r"^! No configuration change since last restart",
    r"^ntp clock-period",
)
_ASA_VOLATILE = (
    r"^Cryptochecksum:",
    r"^: Written by",
    r"^: Saved",
)

PLATFORMS: dict[str, Platform] = {p.key: p for p in (
    Platform("cisco_ios", "Cisco IOS / IOS-XE", "cisco_ios", volatile_lines=_IOS_VOLATILE),
    Platform("cisco_nxos", "Cisco NX-OS", "cisco_nxos", needs_enable=False,
             volatile_lines=(r"^!Time:", r"^!Running configuration last done at:",
                             r"^!Command: show running-config")),
    Platform("cisco_asa", "Cisco ASA / Firepower (ASA mode)", "cisco_asa",
             volatile_lines=_ASA_VOLATILE),
    Platform("cisco_ftd", "Cisco Firepower Threat Defense (FTD)", "cisco_ftd",
             needs_enable=False, volatile_lines=_ASA_VOLATILE),
    Platform("allied_awplus", "Allied Telesis AlliedWare Plus", "allied_telesis_awplus",
             volatile_lines=(r"^Building configuration", r"^Current configuration\s*:")),
)}
