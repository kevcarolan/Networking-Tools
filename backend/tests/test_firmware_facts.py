from types import SimpleNamespace

import pytest

from app.tools.firmware_upgrade.facts import (best_standard, compare_versions, compliance,
                                              parse_facts)
from tests import firmware_samples as s


def test_ios_xe_install_mode():
    f = parse_facts("cisco_ios", [s.IOS_XE_VERSION, s.IOS_DIR])
    assert (f.version, f.model, f.serial) == ("17.09.04a", "C9300-48P", "FOC2330X0AB")
    assert f.boot_mode == "install" and f.image == "flash:packages.conf"
    assert (f.flash_total, f.flash_free) == (11353194496, 8151625728)


def test_ios_classic():
    f = parse_facts("cisco_ios", [s.IOS_CLASSIC_VERSION, s.INVALID])
    assert (f.version, f.model, f.serial) == ("15.2(7)E8", "WS-C2960X-48FPD-L", "FOC1234X5YZ")
    assert f.boot_mode == "bundle" and f.flash_free is None


def test_nxos():
    f = parse_facts("cisco_nxos", [s.NXOS_VERSION, s.NXOS_DIR])
    assert (f.version, f.model, f.serial) == ("9.3(10)", "C93180YC-EX", "FDO21120ABC")
    assert f.image == "bootflash:///nxos.9.3.10.bin"
    assert (f.flash_total, f.flash_free) == (53376499712, 47335612416)


def test_asa_with_failover():
    f = parse_facts("cisco_asa", [s.ASA_VERSION, s.ASA_DIR, s.ASA_FAILOVER])
    assert (f.version, f.model, f.serial) == ("9.18(4)", "FPR-2110", "JAD23456ABC")
    assert f.ha_role == "Primary / Active"
    assert f.flash_free == 7979917312


def test_asa_standalone():
    f = parse_facts("cisco_asa", [s.ASA_VERSION, s.ASA_DIR, "Failover Off\n"])
    assert f.ha_role == "standalone"


def test_ftd():
    f = parse_facts("cisco_ftd", [s.FTD_VERSION, "Failover Off\n"])
    assert (f.version, f.model, f.serial) == ("7.2.5", "FPR-2110", "JAD98765XYZ")
    assert f.image == "build 208" and f.ha_role == "standalone"


def test_awplus():
    f = parse_facts("allied_awplus", [s.AWPLUS_VERSION, s.AWPLUS_SYSTEM])
    assert (f.version, f.model, f.serial) == ("5.5.2-0.4", "x930-28GTX", "A04563H182400035")
    assert f.image == "x930-5.5.2-0.4.rel"


def test_awplus_without_show_system():
    f = parse_facts("allied_awplus", [s.AWPLUS_VERSION, s.INVALID])
    assert f.version == "5.5.2-0.4" and f.model == "x930"


@pytest.mark.parametrize("outputs", [[s.INVALID, ""], ["nothing useful here\n", ""]])
def test_rejects_unusable_output(outputs):
    with pytest.raises(ValueError):
        parse_facts("cisco_ios", outputs)


@pytest.mark.parametrize("a, b, expected", [
    ("17.09.04a", "17.9.4a", 0),
    ("17.9.4", "17.9.4a", -1),
    ("17.12.1", "17.9.4a", 1),
    ("9.16(4)", "9.16(4)23", -1),
    ("15.2(7)E8", "15.2(7)E3", 1),
    ("9.3(10)", "9.3(9)", 1),
    ("5.5.2-0.4", "5.5.2-0.4", 0),
])
def test_compare_versions(a, b, expected):
    assert compare_versions(a, b) == expected


def test_best_standard_prefers_most_specific_pattern():
    std = lambda i, pattern, plat="cisco_ios": SimpleNamespace(  # noqa: E731
        id=i, platform=plat, model_pattern=pattern)
    standards = [std(1, "*"), std(2, "C9300-*"), std(3, "C9300-48*"), std(4, "*", "cisco_nxos")]
    assert best_standard(standards, "cisco_ios", "C9300-48P").id == 3
    assert best_standard(standards, "cisco_ios", "c9300-24t").id == 2
    assert best_standard(standards, "cisco_ios", "WS-C2960X-48FPD-L").id == 1
    assert best_standard(standards, "cisco_ios", None).id == 1
    assert best_standard(standards, "cisco_asa", "FPR-2110") is None


def test_compliance():
    assert compliance("17.09.04a", "17.9.4a") == "compliant"
    assert compliance("17.6.5", "17.9.4a") == "behind"
    assert compliance("17.12.1", "17.9.4a") == "ahead"
    assert compliance(None, "17.9.4a") == "unknown"
    assert compliance("17.6.5", None) == "no_standard"
