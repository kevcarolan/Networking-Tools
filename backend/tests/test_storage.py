import pytest

from app.tools.config_backup.storage import GitConfigStore, config_path_for


def test_config_path_is_safe():
    assert config_path_for("HQ Site", "core-sw1") == "HQ-Site/core-sw1.cfg"
    assert config_path_for("", "../../etc") == "unassigned/etc.cfg"


def test_save_history_diff_and_rename(tmp_path):
    store = GitConfigStore(tmp_path / "configs")
    changed, first = store.save("HQ/sw1.cfg", "hostname sw1\n", "sw1: backup")
    assert changed and first
    assert store.save("HQ/sw1.cfg", "hostname sw1\n", "sw1: backup") == (False, first)

    changed, second = store.save("HQ/sw1.cfg", "hostname sw1\nvlan 10\n", "sw1: backup")
    assert changed and second != first

    # Device moved to another site: file is renamed and history follows it.
    changed, _ = store.save("DC/sw1.cfg", "hostname sw1\nvlan 10\n", "sw1: backup",
                            previous_path="HQ/sw1.cfg")
    assert not changed
    assert not (tmp_path / "configs/HQ/sw1.cfg").exists()
    history = store.history("DC/sw1.cfg")
    assert [v["commit"] for v in history][-2:] == [second, first]
    assert history[-1]["path"] == "HQ/sw1.cfg"

    diff = store.diff((history[-1]["path"], first), ("DC/sw1.cfg", history[0]["commit"]))
    assert "+vlan 10" in diff
    assert store.read("HQ/sw1.cfg", first) == "hostname sw1\n"


def test_rejects_bad_paths_and_commits(tmp_path):
    store = GitConfigStore(tmp_path / "configs")
    with pytest.raises(ValueError):
        store.save("../escape.cfg", "x", "m")
    with pytest.raises(ValueError):
        store.read("a.cfg", "--output=/tmp/x")
