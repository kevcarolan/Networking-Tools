"""Low-level pieces of the upgrade drivers: answering device prompts, FDM certificate
pinning (against a real local TLS server), and the platform parsers."""

import datetime
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.ssh import DeviceError
from app.tools.firmware_upgrade import fdm
from app.tools.firmware_upgrade.deviceio import RELOAD_CLOSED, _interactive
from app.tools.firmware_upgrade.drivers.allied_awplus import file_size
from app.tools.firmware_upgrade.drivers.cisco_ftd import version_matches
from app.tools.firmware_upgrade.drivers.cisco_nxos import impact_ok, parse_vpc, vpc_healthy


class FakeChannel:
    """Plays back what a device would print, one chunk per read; records what we send."""

    def __init__(self, chunks, close_after=False):
        self.chunks, self.sent, self.close_after = list(chunks), [], close_after
        self.base_prompt, self.RETURN = "sw1", "\n"
        self.closed = False
        chan = self

        class Transport:
            def is_active(self):
                return not chan.closed

        class Remote:
            def get_transport(self):
                return Transport()

        self.remote_conn = Remote()

    def write_channel(self, data):
        self.sent.append(data)

    def read_channel(self):
        if self.chunks:
            return self.chunks.pop(0)
        if self.close_after:
            self.closed = True
        return ""


def test_reload_answers_the_questions_and_treats_the_drop_as_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ch = FakeChannel(["reload\nSystem configuration has been modified. Save? [yes/no]: ",
                      "Building configuration...\n[OK]\nProceed with reload? [confirm]",
                      "\n*Reloading*"], close_after=True)
    out = _interactive(ch, "reload", 60, expect_reload=True)
    assert out.endswith(RELOAD_CLOSED)
    assert ch.sent == ["reload\n", "yes\n", "\n"]


def test_command_returns_at_the_prompt_and_a_drop_is_an_error_otherwise(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ch = FakeChannel(["install commit\n", "SUCCESS: install_commit\nsw1#"])
    assert "SUCCESS" in _interactive(ch, "install commit", 60, expect_reload=False)
    ch = FakeChannel(["install commit\n"], close_after=True)
    with pytest.raises(DeviceError):
        _interactive(ch, "install commit", 60, expect_reload=False)


# --- FDM certificate pinning ------------------------------------------------------------

@pytest.fixture
def tls_server(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fdm.lab")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            seen.append((self.path, self.rfile.read(length)))
            self._reply({"access_token": "tok"})

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self._reply({"softwareVersion": "7.2.5-208"})

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    fp = fdm.format_fp(cert.fingerprint(hashes.SHA256()).hex())
    yield httpd.server_address[1], fp, seen
    httpd.shutdown()


def test_fdm_client_pins_the_certificate(tls_server):
    port, fp, seen = tls_server
    assert fdm.fetch_fingerprint("127.0.0.1", port) == fp
    client = fdm.FdmClient("127.0.0.1", "admin", "pw", fp.lower().replace(":", ""), port=port)
    client.login()
    assert client.system_info()["softwareVersion"] == "7.2.5-208"
    assert seen[0][0] == "/api/fdm/latest/fdm/token" and b'"grant_type": "password"' in seen[0][1]
    assert seen[1] == ("/api/fdm/latest/operational/systeminfo/default", "Bearer tok")

    wrong = fdm.FdmClient("127.0.0.1", "admin", "pw", "00" * 32, port=port)
    with pytest.raises(DeviceError, match="certificate changed"):
        wrong.login()
    with pytest.raises(DeviceError, match="isn't trusted"):
        fdm.FdmClient("127.0.0.1", "admin", "pw", "")


# --- parsers ---------------------------------------------------------------------------

def test_platform_parsers():
    good = ("Compatibility check is done:\nModule  bootable          Impact  Install-type  Reason\n"
            "------  --------  --------------  ------------  ------\n"
            "     1       yes      disruptive         reset  default upgrade is not hitless\n")
    assert impact_ok(good)
    assert not impact_ok(good.replace("yes", "no"))
    assert not impact_ok("Pre-upgrade check failed. Return code 0x40930011")
    v = parse_vpc("Peer status : peer adjacency formed ok\nvPC keep-alive status : peer is alive\n"
                  "Configuration consistency status : success\nvPC role : primary, operational secondary\n")
    assert vpc_healthy(v) and v["role"].startswith("primary")
    assert not vpc_healthy({**v, "keepalive": "peer is not reachable through peer keepalive"})
    assert file_size("   45563908 -rw- Mar 10 2024 10:01:02  x930-5.5.4-1.1.rel\n",
                     "x930-5.5.4-1.1.rel") == 45563908
    assert file_size("", "x.rel") is None
    assert version_matches("7.4.1-172", "7.4.1") and not version_matches("7.4.2-10", "7.4.1")
