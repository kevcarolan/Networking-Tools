import pytest

from app.tools.firmware_upgrade import checks as c
from tests import firmware_samples as s

IOS_GOOD = {
    "version": s.IOS_XE_VERSION, "dir": s.IOS_DIR, "alarms": s.ALARMS_OK, "env": s.ENV_OK,
    "cpu": s.CPU_OK, "mem": s.MEM_OK, "install": s.INSTALL_OK, "unsaved": s.UNSAVED_NONE,
    "stack": s.STACK_OK, "snap:interfaces": s.IP_BRIEF, "snap:cdp": s.CDP_DETAIL,
    "snap:lldp": s.LLDP_DETAIL, "snap:etherchannel": s.ETHERCHANNEL, "snap:mac": s.MAC_COUNT,
}
IMAGE = c.ImageFacts(platform="cisco_ios", version="17.12.04", model_pattern="C9300-*",
                     size=1_200 * 2**20, md5_ok=True, filename="cat9k_iosxe.17.12.04.SPA.bin")


def _by_id(results):
    return {r.check_id: r for r in results}


def _pre(outputs=IOS_GOOD, image=IMAGE):
    results, facts = c.pre_checks("cisco_ios", outputs, image, c.Thresholds(), 1.1, 2.2)
    return _by_id(results), facts


def test_parsers():
    a = c.parse_alarms(s.ALARMS_BAD)
    assert (a["critical"], a["major"], a["minor"]) == (1, 0, 1)
    assert a["rows"][0].startswith("CRITICAL: Power Supply Bay 2 Failed")
    assert c.parse_alarms(s.ALARMS_OK)["rows"] == []
    assert c.parse_environment(s.ENV_OK) == []
    assert "FAILED" in c.parse_environment(s.ENV_BAD)[0]
    assert c.parse_cpu(s.CPU_OK) == 7 and c.parse_cpu(s.ASA_CPU) == 3
    assert c.parse_memory(s.MEM_OK) == 25 and c.parse_memory(s.ASA_MEM) == 25
    ins = c.parse_install_summary(s.INSTALL_OK)
    assert ins["pending"] == [] and ins["inactive"] == ["17.06.05.0.1234"]
    assert ins["abort_timer"] == "inactive"
    pend = c.parse_install_summary(s.INSTALL_PENDING)
    assert pend["pending"] == ["U 17.12.04.0.11"] and pend["abort_timer"] == "active"
    assert c.parse_unsaved(s.UNSAVED_NONE) == 0 and c.parse_unsaved(s.UNSAVED_SOME) == 2
    assert c.parse_stack(s.STACK_OK) == {"1": "Ready", "2": "Ready"}
    assert c.parse_stack(s.STACK_BAD)["2"] == "Removed"
    fo = c.parse_failover(s.ASA_FAILOVER)
    assert fo["this"] == "Active" and fo["other"] == "Standby Ready"
    assert c.parse_failover("Failover Off\n") == {"enabled": False}
    ib = c.parse_ip_brief(s.IP_BRIEF)
    assert ib["Vlan1"] == "admin-down" and ib["GigabitEthernet1/0/2"] == "down"
    assert ib["GigabitEthernet2/0/13"] == "up"
    assert c.parse_cdp_detail(s.CDP_DETAIL) == ["GigabitEthernet1/0/1 > DUB01-CORE-01",
                                                "GigabitEthernet1/1/1 > GH-AS02"]
    assert c.parse_lldp_detail(s.LLDP_DETAIL) == ["Gi1/0/1 > DUB01-CORE-01",
                                                  "Gi2/0/13 > A-DUB01-GH-100-3"]
    assert c.parse_etherchannel(s.ETHERCHANNEL) == {"Po1": ["Gi1/1/1", "Gi2/1/1"]}
    assert c.parse_mac_count(s.MAC_COUNT) == 52


def test_pre_checks_all_pass_on_a_healthy_switch():
    r, facts = _pre()
    assert facts["version"] == "17.09.04a" and facts["boot_mode"] == "install"
    blocking = [x for x in r.values() if x.blocking]
    assert blocking == []
    for cid in ("alarms", "environment", "install_state", "stack", "unsaved_config", "flash_space",
                "image_model", "version_differs", "install_mode", "image_md5", "image_platform"):
        assert r[cid].status == c.PASS, cid
    assert r["install_inactive"].severity == c.INFO
    assert "MB free" in r["flash_space"].value


@pytest.mark.parametrize("key, value, check_id", [
    ("alarms", s.ALARMS_BAD, "alarms"),
    ("env", s.ENV_BAD, "environment"),
    ("install", s.INSTALL_PENDING, "install_state"),
    ("unsaved", s.UNSAVED_SOME, "unsaved_config"),
    ("stack", s.STACK_BAD, "stack"),
])
def test_blockers(key, value, check_id):
    r, _ = _pre({**IOS_GOOD, key: value})
    assert r[check_id].blocking, r[check_id]


def test_warnings_do_not_block():
    r, _ = _pre({**IOS_GOOD, "cpu": s.CPU_HIGH, "alarms": s.ALARMS_OK.replace(
        "Minor: 0", "Minor: 1") + "Switch 2   Oct 01 2026 09:13:10   MINOR   Fan slow [1]\n"})
    assert r["cpu"].status == c.FAIL and not r["cpu"].blocking
    assert r["alarms_minor"].status == c.FAIL and not r["alarms_minor"].blocking
    assert not r["alarms"].blocking


def test_image_checks():
    r, _ = _pre(image=c.ImageFacts("cisco_ios", "17.09.04a", "C9500-*", 1, False, "x.bin"))
    assert r["version_differs"].blocking          # already on that version
    assert r["image_model"].blocking               # wrong model
    assert r["image_md5"].blocking                 # file changed on disk
    big = c.ImageFacts("cisco_ios", "17.12.04", "*", 5_000 * 2**20, True, "x.bin")
    assert _pre(image=big)[0]["flash_space"].blocking
    assert _pre(image=c.ImageFacts("cisco_nxos", "1", "*", 1, True, "x"))[0]["image_platform"].blocking


def test_bundle_mode_blocks_and_unsupported_commands_skip():
    bundle = s.IOS_XE_VERSION.replace("packages.conf", "cat9k.bin").replace(
        "CAT9K_IOSXE           INSTALL", "CAT9K_IOSXE           BUNDLE")
    r, _ = _pre({**IOS_GOOD, "version": bundle})
    assert r["install_mode"].blocking
    r, _ = _pre({**IOS_GOOD, "alarms": s.INVALID, "install": s.INVALID})
    assert r["alarms"].status == c.SKIP and not r["alarms"].blocking
    assert r["install_state"].status == c.SKIP


def test_asa_failover_check():
    out = {"version": s.ASA_VERSION, "dir": s.ASA_DIR, "failover": s.ASA_FAILOVER,
           "cpu": s.ASA_CPU, "mem": s.ASA_MEM}
    img = c.ImageFacts("cisco_asa", "9.20(2)", "FPR-*", 300 * 2**20, True, "asa.SPA")
    r = _by_id(c.pre_checks("cisco_asa", out, img, c.Thresholds(), 1.1, 2.2)[0])
    assert r["failover"].status == c.PASS and r["cpu"].value == "3%"
    r = _by_id(c.pre_checks("cisco_asa", {**out, "failover": s.ASA_FAILOVER_BAD}, img,
                            c.Thresholds(), 1.1, 2.2)[0])
    assert r["failover"].blocking


def test_other_platforms_get_common_checks_and_an_info_note():
    out = {"version": s.NXOS_VERSION, "dir": s.NXOS_DIR}
    img = c.ImageFacts("cisco_nxos", "10.3(5)", "*", 2_000 * 2**20, True, "nxos.bin")
    r = _by_id(c.pre_checks("cisco_nxos", out, img, c.Thresholds(), 1.1, 2.2)[0])
    assert r["flash_space"].status == c.PASS and r["platform_checks"].severity == c.INFO


def test_snapshot_and_compare():
    pre = c.snapshot("cisco_ios", IOS_GOOD)
    assert pre["version"] == "17.09.04a" and pre["mac_count"] == 52
    assert pre["interfaces"]["GigabitEthernet2/0/13"] == "up" and len(pre["stack"]) == 2

    after = {**IOS_GOOD, "version": s.IOS_XE_VERSION.replace("17.09.04a", "17.12.04")}
    ok = _by_id(c.compare_snapshots(pre, c.snapshot("cisco_ios", after), "17.12.04"))
    assert all(r.status == c.PASS for r in ok.values()), ok

    broken = {**after,
              "snap:interfaces": s.IP_BRIEF.replace("GigabitEthernet2/0/13  unassigned      YES unset  up                    up",
                                                    "GigabitEthernet2/0/13  unassigned      YES unset  down                  down"),
              "snap:cdp": s.CDP_DETAIL.split("-------------------------\nDevice ID: GH-AS02")[0],
              "snap:etherchannel": s.ETHERCHANNEL.replace("Gi2/1/1(P)", "Gi2/1/1(D)"),
              "snap:mac": s.MAC_COUNT.replace("criterion: 52", "criterion: 10"),
              "stack": s.STACK_OK.replace(" 2       Standby  0011.2233.4466     14     V01     Ready\n", "")}
    bad = _by_id(c.compare_snapshots(pre, c.snapshot("cisco_ios", broken), "17.12.04"))
    assert bad["post_interfaces"].blocking and "GigabitEthernet2/0/13" in bad["post_interfaces"].detail
    assert bad["post_cdp"].blocking and "GH-AS02" in bad["post_cdp"].detail
    assert bad["post_etherchannel"].blocking and bad["post_stack"].blocking
    assert bad["post_mac"].status == c.FAIL and not bad["post_mac"].blocking
    wrong = _by_id(c.compare_snapshots(pre, pre, "17.12.04"))
    assert wrong["post_version"].blocking
