"""Cisco NX-OS: install all nxos bootflash:<image>.

Standalone, or a vPC pair: the vPC secondary first, check the vPC is healthy
again, then the primary. Roll back: install all with the previous image.
"""

import re

from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.drivers.base import Step, StepFailed, Unit, UpgradeDriver, UpgradePath

_INSTALL_FAILED = re.compile(r"Install has failed|Pre-upgrade check failed|failed|Error", re.I)


def parse_vpc(out: str) -> dict:
    get = lambda pat: (re.search(pat, out or "", re.I | re.M) or [None, ""])[1].strip()  # noqa: E731
    return {"peer": get(r"Peer status\s*:\s*(.+)$"), "keepalive": get(r"keep-alive status\s*:\s*(.+)$"),
            "role": get(r"vPC role\s*:\s*(.+)$"), "consistency": get(r"Configuration consistency status\s*:\s*(.+)$")}


def impact_ok(out: str) -> bool:
    """'show install all impact': the check finished and every module is bootable.
        Module  bootable          Impact  Install-type  Reason
        ------  --------  --------------  ------------  ------
             1       yes      disruptive         reset  default upgrade is not hitless"""
    if not re.search(r"Compatibility check is done", out or "", re.I):
        return False
    rows = re.findall(r"^\s*\d+\s+(yes|no)\b", out, re.M | re.I)
    return bool(rows) and all(r.lower() == "yes" for r in rows)


def vpc_healthy(v: dict) -> bool:
    return ("adjacency formed ok" in v["peer"].lower() and "peer is alive" in v["keepalive"].lower()
            and (not v["consistency"] or v["consistency"].lower() == "success"))


class CiscoNxosDriver(UpgradeDriver):
    platform = "cisco_nxos"
    default_fs = "bootflash:"
    paths = [
        UpgradePath("standalone", "Standalone switch", 1, "install all nxos"),
        UpgradePath("vpc_pair", "vPC pair (secondary first)", 2,
                    "Upgrade the vPC secondary, check the vPC, then the primary"),
    ]

    def extra_commands(self, unit, image):
        return {"fw:scp": "show running-config | include scp-server",
                "fw:vpc": "show vpc brief"}

    def verify_command(self, u, filename):
        return f"show file {self.file_system(u)}{filename} md5sum"

    def extra_prechecks(self, ctx, unit: Unit, outputs, path):
        results = self.image_on_device_checks(ctx, unit, outputs)
        scp = "feature scp-server" in (outputs.get("fw:scp") or "")
        results.append(ck.CheckResult(
            "scp_server", "SCP server enabled (for the image copy)", ck.BLOCKER,
            ck.PASS if scp else ck.FAIL, "enabled" if scp else "not enabled",
            "" if scp else "Configure 'feature scp-server'"))
        unit.running_image = unit.facts.get("image") or ""
        old = unit.running_image.replace("bootflash:///", "").replace("bootflash:", "")
        present = bool(old) and old in (outputs.get("dir") or "")
        results.append(ck.CheckResult(
            "previous_image", "Current image stays on bootflash (for roll back)", ck.BLOCKER,
            ck.PASS if present else ck.FAIL, unit.running_image or "unknown",
            "" if present else "The running image isn't on bootflash"))
        if path.units == 2:
            v = parse_vpc(outputs.get("fw:vpc") or "")
            unit.role = v["role"].split()[0].strip(",").lower() if v["role"] else ""
            results.append(ck.CheckResult(
                "vpc", "vPC peer link and keep-alive healthy", ck.BLOCKER,
                ck.PASS if vpc_healthy(v) else ck.FAIL, v["role"] or "no vPC",
                f"peer: {v['peer']}\nkeep-alive: {v['keepalive']}\nconsistency: {v['consistency']}"))
        return results

    def pair_checks(self, ctx, units, path):
        roles = sorted(u.role for u in units)
        ok = roles == ["primary", "secondary"]
        return [ck.CheckResult("pair_roles", "One vPC primary and one secondary", ck.BLOCKER,
                               ck.PASS if ok else ck.FAIL, ", ".join(f"{u.name}: {u.role or '?'}"
                                                                     for u in units))]

    def order_units(self, units: list[Unit]) -> list[Unit]:
        return sorted(units, key=lambda u: 0 if "secondary" in u.role else 1)

    def upgrade_plan(self, ctx, path, units):
        steps: list[Step] = []
        for i, u in enumerate(units):
            if i:
                steps.append(Step(f"vpc_check:{u.device_id}", "vpc_check",
                                  "vPC healthy again before the second switch", units[0].device_id))
            steps += self.unit_steps(ctx, u)
        return steps

    def activate_steps(self, ctx, u):
        d = u.device_id
        return [Step(f"impact:{d}", "impact", f"{u.name}: show install all impact", d),
                Step(f"activate:{d}", "install_all", f"{u.name}: install all nxos (reloads)", d,
                     reload=True)]

    def rollback_steps(self, ctx, u):
        return [Step(f"rb:{u.device_id}", "install_all", f"{u.name}: install all with the "
                     "previous image (reloads)", u.device_id, reload=True, args={"previous": True})]

    def step_impact(self, ctx, step, u):
        cmd = f"show install all impact nxos {self.file_system(u)}{ctx.image.filename}"
        if ctx.dry_run and not u.staged:
            ctx.event(f"DRY RUN - would run: {cmd}", step.kind, "action", u.device_id)
            return
        out = ctx.show(u, [cmd], long=True)[0]
        ok = impact_ok(out)
        ctx.record_checks(u, "stage", [ck.CheckResult(
            "install_impact", "install all impact: image is compatible", ck.BLOCKER,
            ck.PASS if ok else ck.FAIL, "compatible" if ok else "not compatible",
            out.strip()[-800:])])
        if not ok:
            raise StepFailed(f"'show install all impact' reports a problem on {u.name}")

    def step_install_all(self, ctx, step, u):
        image = u.running_image if step.args.get("previous") else \
            f"{self.file_system(u)}{ctx.image.filename}"
        u.status = "activated"
        out = ctx.change(u, [f"install all nxos {image} non-interruptive"], expect_reload=True,
                         long=True)
        if not ctx.dry_run and _INSTALL_FAILED.search(out.split("[connection closed")[0]):
            raise StepFailed(f"install all failed on {u.name}: {out.strip()[-400:]}",
                             attention=True)

    def step_vpc_check(self, ctx, step, u):
        out = ctx.show(u, ["show vpc brief"])[0]
        v = parse_vpc(out)
        if not vpc_healthy(v):
            raise StepFailed(f"The vPC isn't healthy after the first switch: peer {v['peer']}, "
                             f"keep-alive {v['keepalive']}")
        ctx.event("vPC healthy - carrying on with the second switch", step.kind,
                  device_id=u.device_id)
