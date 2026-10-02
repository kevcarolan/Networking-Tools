"""A small network simulator for the upgrade tests.

FakeNetwork stands in for DeviceIO (SSH show/change/copy and the FDM API). Each
SimDevice keeps a version, files on flash, boot settings, install state and an HA
role; reloads take simulated time, which only passes when the engine sleeps (the
engine's sleep/monotonic are the network's). Failures can be injected per device:

    copy_md5     the copied file has the wrong checksum
    install_add  'install add' reports FAILED
    activate     'install activate' fails before reloading
    no_return    the device never comes back after a reload
    impact       NX-OS 'show install all impact' reports an incompatible image
    readiness    FTD readiness check fails
"""

import hashlib
from pathlib import Path

from app.core.ssh import SETUP, UNREACHABLE, DeviceError
from app.tools.firmware_upgrade.deviceio import RELOAD_CLOSED, DeviceIO
from tests import firmware_samples as s

FP = "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"


def md5_of(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()  # noqa: S324


IOS_HEALTH = {
    "show facility-alarm status": s.ALARMS_OK, "show environment all": s.ENV_OK,
    "show processes cpu | include CPU utilization": s.CPU_OK,
    "show processes memory | include Processor Pool": s.MEM_OK,
    "show install summary": s.INSTALL_OK, "show archive config differences": s.UNSAVED_NONE,
    "show switch": s.STACK_OK, "show ip interface brief": s.IP_BRIEF,
    "show cdp neighbors detail": s.CDP_DETAIL, "show lldp neighbors detail": s.LLDP_DETAIL,
    "show etherchannel summary": s.ETHERCHANNEL, "show mac address-table count": s.MAC_COUNT,
    "show running-config | include ip scp server": "ip scp server enable\n",
    "show running-config | include ^boot system": "",
    "show install rollback": "ID  Label     Description\n--  --------  -----------\n"
                             "1   No Label  No Description\n",
}


class SimDevice:
    def __init__(self, name: str, platform: str, version: str, image: str, outputs=None):
        self.name, self.platform, self.version, self.image = name, platform, version, image
        self.outputs = dict(outputs or {})
        self.files: dict[str, tuple[int, str]] = {}
        self.up, self.down_for, self.next = True, 0.0, None
        self.boot: list[str] = []
        self.added = None
        self.uncommitted = False
        self.previous = None  # (version, image) before the last activation
        self.role = ""
        self.peer: "SimDevice | None" = None
        self.fail: set[str] = set()
        self.commands: list[str] = []
        self.reloads = 0
        self.fingerprint = FP
        self.fdm_files: list[dict] = []
        self.pending: list = []
        self.readiness = ""
        self.after_reload: dict[str, str] = {}  # outputs that change once it has reloaded

    # --- show output ---------------------------------------------------------------

    def version_text(self) -> str:
        v = self.version
        if self.platform == "cisco_ios":
            if self.image.endswith("packages.conf"):
                return s.IOS_XE_VERSION.replace("17.09.04a", v).replace("17.9.4a", v) \
                    .replace("core-sw1", self.name)
            return s.IOS_CLASSIC_VERSION.replace("15.2(7)E8", v).replace(
                "flash:/c2960x-universalk9-mz.152-7.E8/c2960x-universalk9-mz.152-7.E8.bin", self.image)
        if self.platform == "cisco_nxos":
            return s.NXOS_VERSION.replace("9.3(10)", v).replace("bootflash:///nxos.9.3.10.bin",
                                                                 self.image)
        if self.platform == "cisco_asa":
            return s.ASA_VERSION.replace("9.18(4)", v).replace("disk0:/cisco-asa-fp2k.9.18.4.SPA",
                                                                self.image)
        if self.platform == "cisco_ftd":
            return s.FTD_VERSION.replace("Version 7.2.5", f"Version {v}")
        if self.platform == "allied_awplus":
            return s.AWPLUS_VERSION.replace("5.5.2 ", f"{v} ").replace("x930-5.5.2-0.4.rel",
                                                                        self.image.split("/")[-1])
        return f"Version {v}"

    def dir_text(self) -> str:
        base = {"cisco_ios": s.IOS_DIR, "cisco_nxos": s.NXOS_DIR, "cisco_asa": s.ASA_DIR}.get(
            self.platform, "")
        lines = "".join(f"  {size}  -rw-  Jan 1 2024 10:00:00  {name}\n"
                        for name, (size, _md5) in self.files.items())
        if self.platform == "allied_awplus":
            running = self.image.split("/")[-1]
            return f"   45563908 -rw- Mar 10 2024 10:01:02  {running}\n" + lines
        return base + lines

    def failover_text(self) -> str:
        if not self.role:
            return "Failover Off\n"
        other_ok = self.peer is not None and self.peer.up
        this = "Active" if self.role == "active" else "Standby Ready"
        other = ("Standby Ready" if self.role == "active" else "Active") if other_ok else "Failed"
        return (f"Failover On\nFailover unit Primary\n        This host: Primary - {this}\n"
                f"        Other host: Secondary - {other}\n")

    def output(self, cmd: str) -> str:
        if cmd == "show version":
            return self.version_text()
        if self.platform == "allied_awplus" and cmd.startswith("dir flash:/"):
            name = cmd.split("/")[-1]
            if name not in self.files:
                return "% No such file or directory\n"
            return f"   {self.files[name][0]} -rw- Mar 10 2024 10:01:02  {name}\n"
        if cmd in ("dir", "dir flash:", "dir bootflash:", "dir disk0:"):
            return self.dir_text()
        if cmd.startswith("verify /md5 "):
            path = cmd.split()[2]
            name = path.split(":")[-1].lstrip("/")
            if name in self.files:
                return f"..........Done!\nverify /md5 ({path}) = {self.files[name][1]}\n"
            return f"%Error opening {path} (No such file or directory)\n"
        if cmd.startswith("show file ") and cmd.endswith(" md5sum"):
            name = cmd.split()[2].split(":")[-1].lstrip("/")
            return (self.files[name][1] + "\n") if name in self.files else "No such file\n"
        if cmd.startswith("show install all impact"):
            if "impact" in self.fail:
                return "Compatibility check is done:\nModule  bootable  Impact\n1  no  Incompatible image\n"
            return "Compatibility check is done:\nModule  bootable  Impact  Install-type\n" \
                   "------  --------  ------  ------------\n     1       yes  disruptive  reset\n"
        if cmd == "show failover":
            return self.failover_text()
        if cmd == "show vpc brief":
            peer_ok = self.peer is not None and self.peer.up
            return (f"vPC domain id : 10\nPeer status : "
                    f"{'peer adjacency formed ok' if peer_ok else 'peer link is down'}\n"
                    f"vPC keep-alive status : {'peer is alive' if peer_ok else 'peer is not reachable'}\n"
                    f"Configuration consistency status : success\nvPC role : {self.role}\n")
        return self.outputs.get(cmd, s.INVALID)


class FakeFdm:
    def __init__(self, net: "FakeNetwork", dev: SimDevice):
        self.net, self.dev = net, dev

    def _up(self):
        if not self.dev.up:
            raise DeviceError(UNREACHABLE, "FDM API unreachable: device down")

    def _log(self, what):
        self.dev.commands.append(what)
        self.net.log.append((self.dev.name, what))

    def login(self):
        self._up()

    def system_info(self):
        self._up()
        return {"softwareVersion": f"{self.dev.version}-208"}

    def pending_changes(self):
        self._up()
        return list(self.dev.pending)

    def ha_status(self):
        self._up()
        if not self.dev.role:
            return {"nodeState": "SINGLE_NODE"}
        peer_ok = self.dev.peer is not None and self.dev.peer.up
        me = "HA_ACTIVE_NODE" if self.dev.role == "active" else "HA_STANDBY_NODE"
        peer = ("HA_STANDBY_NODE" if self.dev.role == "active" else "HA_ACTIVE_NODE") if peer_ok \
            else "HA_FAILED_NODE"
        return {"nodeState": me, "peerNodeState": peer, "configStatus": "IN_SYNC"}

    def upgrade_files(self):
        self._up()
        return list(self.dev.fdm_files)

    def upgrade_status(self):
        self._up()
        return {"readinessCheckState": self.dev.readiness, "state": self.dev.readiness}

    def upload(self, local: Path):
        self._up()
        self._log(f"fdm:upload {local.name}")
        self.dev.fdm_files.append({"id": f"file-{len(self.dev.fdm_files) + 1}",
                                   "upgradeFileName": local.name})
        return {}

    def readiness_check(self, file_id):
        self._up()
        self._log(f"fdm:readiness {file_id}")
        self.dev.readiness = "FAILED" if "readiness" in self.dev.fail else "SUCCESS"
        return {}

    def start_upgrade(self, file_id):
        self._up()
        name = next(f["upgradeFileName"] for f in self.dev.fdm_files if f["id"] == file_id)
        self._log(f"fdm:upgrade {name}")
        self.net.reload(self.dev, self.net.versions[name])
        return {}

    def ha_failover(self):
        self._up()
        self._log("fdm:ha_failover")
        peer = self.dev.peer
        self.dev.role, peer.role = peer.role, self.dev.role
        return {}

    def revert(self):
        self._up()
        self._log("fdm:revert")
        self.net.reload(self.dev, self.dev.previous[0] if self.dev.previous else self.dev.version)
        return {}


class FakeNetwork(DeviceIO):
    def __init__(self, versions: dict[str, str] | None = None, reload_seconds: float = 300):
        super().__init__(collector=None)
        self.devices: dict[str, SimDevice] = {}
        self.versions = dict(versions or {})  # image file name -> version it installs
        self.reload_seconds = reload_seconds
        self.clock = 0.0
        self.log: list[tuple[str, str]] = []  # (device, change) in the order they happened

    def add(self, dev: SimDevice) -> SimDevice:
        self.devices[dev.name] = dev
        return dev

    # --- time ----------------------------------------------------------------------

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += max(seconds, 1)
        for d in self.devices.values():
            if not d.up and "no_return" not in d.fail:
                d.down_for -= max(seconds, 1)
                if d.down_for <= 0:
                    d.up = True
                    if d.next:
                        d.version, d.image = d.next
                        d.next = None
                    d.outputs.update(d.after_reload)

    def reload(self, d: SimDevice, version: str, image: str | None = None) -> None:
        self.versions.setdefault(d.image.split(":")[-1].lstrip("/"), d.version)
        d.previous = (d.version, d.image)
        d.up, d.down_for, d.next = False, self.reload_seconds, (version, image or d.image)
        d.reloads += 1

    # --- DeviceIO --------------------------------------------------------------------

    def _dev(self, target) -> SimDevice:
        d = self.devices[target.name]
        if not d.up:
            raise DeviceError(UNREACHABLE, "Could not connect over SSH (port 22) - device unreachable?")
        return d

    def show(self, target, commands):
        d = self._dev(target)
        return [d.output(c) for c in commands]

    def reachable(self, target, timeout: float = 5) -> bool:
        return self.devices[target.name].up

    def transfer(self, target, local, file_system, filename, timeout=3600):
        d = self._dev(target)
        d.commands.append(f"copy {filename} -> {file_system}")
        self.log.append((d.name, f"copy {filename}"))
        md5 = "0" * 32 if "copy_md5" in d.fail else md5_of(local)
        d.files[filename] = (local.stat().st_size, md5)
        return "copied"

    def fdm(self, target, fingerprint):
        d = self.devices[target.name]
        if not fingerprint:
            raise DeviceError(SETUP, "The FDM certificate isn't trusted yet")
        if fingerprint.upper() != d.fingerprint:
            raise DeviceError(SETUP, "FDM certificate changed")
        return FakeFdm(self, d)

    def fetch_fingerprint(self, address):
        return FP

    def change(self, target, commands, *, config=False, timeout=600, expect_reload=False):
        d = self._dev(target)
        d.commands += list(commands)
        self.log += [(d.name, c) for c in commands]
        if config:
            for line in commands:
                if line in ("no boot system", "clear configure boot system"):
                    d.boot = []
                elif line.startswith("boot system backup "):
                    pass
                elif line.startswith("boot system "):
                    d.boot.append(line.split(" ", 2)[2])
            if d.platform == "cisco_asa" and d.peer:
                d.peer.boot = list(d.boot)  # configuration replicates to the standby
            return "\n".join(commands)
        return "\n".join(self._exec(d, c) for c in commands)

    def _image_version(self, path: str) -> str:
        name = path.split(":")[-1].lstrip("/")
        return self.versions.get(name) or self.versions.get(path, "?")

    def _exec(self, d: SimDevice, cmd: str) -> str:
        if cmd.startswith("write memory") or cmd == "failover exec mate write memory":
            return "Building configuration...\n[OK]"
        if cmd.startswith("install add file "):
            name = cmd.split()[-1].split(":")[-1]
            if "install_add" in d.fail or name not in d.files:
                return "install_add: FAILED: file not found or corrupt"
            d.added = name
            return f"install_add: START\nSUCCESS: install_add {name}"
        if cmd.startswith("install activate"):
            if "activate" in d.fail:
                return "install_activate: FAILED: insufficient space"
            d.uncommitted = True
            self.reload(d, self.versions[d.added])
            return "install_activate: START\nReloading" + RELOAD_CLOSED
        if cmd == "install commit":
            d.uncommitted = False
            return "SUCCESS: install_commit"
        if cmd.startswith("install abort") or cmd.startswith("install rollback to id"):
            d.uncommitted = False
            self.reload(d, d.previous[0], d.previous[1])
            return "Reloading" + RELOAD_CLOSED
        if cmd.startswith("install all nxos "):
            path = cmd.split()[3]
            self.reload(d, self._image_version(path), path)
            return "Install is in progress, please wait.\nFinishing the upgrade, switch will reboot" \
                + RELOAD_CLOSED
        if cmd in ("reload", "reload noconfirm"):
            boot = d.boot[0] if d.boot else d.image
            self.reload(d, self._image_version(boot), boot)
            return "Proceed with reload? [confirm]" + RELOAD_CLOSED
        if cmd == "failover reload-standby":
            peer = d.peer
            boot = peer.boot[0] if peer.boot else peer.image
            self.reload(peer, self._image_version(boot), boot)
            return ""
        if cmd == "failover active":
            # this unit (standby address) takes over: the physical units swap addresses
            a = d.peer
            for attr in ("version", "image", "files"):
                va, vd = getattr(a, attr), getattr(d, attr)
                setattr(a, attr, vd)
                setattr(d, attr, va)
            return RELOAD_CLOSED
        return ""
