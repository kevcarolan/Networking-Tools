import socket

import pytest
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from app.tools.config_backup.collector import BackupError, classify_error, clean_config
from tests.conftest import SAMPLE_CONFIG


def test_clean_config_strips_volatile_ios_lines():
    raw = SAMPLE_CONFIG.format(name="sw1", address="10.0.0.1").replace("\n", "\r\n")
    cleaned = clean_config("cisco_ios", raw)
    assert "Building configuration" not in cleaned
    assert "Current configuration" not in cleaned
    assert "Last configuration change" not in cleaned
    assert cleaned.startswith("!\n")
    assert "hostname sw1" in cleaned
    assert "\r" not in cleaned and cleaned.endswith("end\n")


def test_clean_config_strips_asa_checksum():
    raw = ": Saved\n:\nASA Version 9.18\nhostname fw1\n!\ninterface Gi0/0\nCryptochecksum:abcd\n: end\n"
    cleaned = clean_config("cisco_ftd", raw)
    assert "Cryptochecksum" not in cleaned and ": Saved" not in cleaned
    assert "hostname fw1" in cleaned


@pytest.mark.parametrize("raw", ["", "\n\n", "% Invalid input detected at '^' marker.\n" * 6])
def test_clean_config_rejects_garbage(raw):
    with pytest.raises(BackupError) as exc:
        clean_config("cisco_ios", raw)
    assert exc.value.error_type == "command"


@pytest.mark.parametrize("exc, expected", [
    (NetmikoAuthenticationException("bad"), "auth"),
    (NetmikoTimeoutException("tcp"), "unreachable"),
    (socket.gaierror("nope"), "unreachable"),
    (ConnectionRefusedError(), "unreachable"),
    (TimeoutError(), "timeout"),
    (RuntimeError("boom"), "error"),
])
def test_classify_error(exc, expected):
    assert classify_error(exc)[0] == expected
