"""Upgrade drivers, one per platform. To support a new design, add a path to a driver
(or a new driver) - the job engine, GUI and report pick it up from here."""

from app.tools.firmware_upgrade.drivers.allied_awplus import AlliedAwplusDriver
from app.tools.firmware_upgrade.drivers.base import UpgradeDriver, UpgradePath
from app.tools.firmware_upgrade.drivers.cisco_asa import CiscoAsaDriver
from app.tools.firmware_upgrade.drivers.cisco_ftd import CiscoFtdDriver
from app.tools.firmware_upgrade.drivers.cisco_ios import CiscoIosDriver
from app.tools.firmware_upgrade.drivers.cisco_nxos import CiscoNxosDriver

DRIVERS: dict[str, UpgradeDriver] = {d.platform: d for d in (
    CiscoIosDriver(), CiscoNxosDriver(), CiscoAsaDriver(), CiscoFtdDriver(), AlliedAwplusDriver())}


def for_platform(platform: str) -> UpgradeDriver | None:
    return DRIVERS.get(platform)


def default_path(platform: str, facts: dict, preferred: str = "", has_peer: bool = False) -> str:
    driver = DRIVERS.get(platform)
    if driver is None:
        return ""
    if preferred and driver.path(preferred):
        return preferred
    if hasattr(driver, "default_path"):
        return driver.default_path(facts)
    pair = next((p.id for p in driver.paths if p.units == 2), None)
    return pair if has_peer and pair else driver.paths[0].id


def catalogue() -> list[dict]:
    return [{"platform": d.platform, "paths": [vars(p) for p in d.paths]} for d in DRIVERS.values()]


__all__ = ["DRIVERS", "UpgradeDriver", "UpgradePath", "catalogue", "default_path", "for_platform"]
