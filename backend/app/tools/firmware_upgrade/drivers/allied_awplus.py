"""Allied Telesis AlliedWare Plus (standalone or VCStack).

The image is pushed over SCP (needs 'ssh server scp'). Then:
    boot system flash:/<new>.rel, boot system backup flash:/<old>.rel, write memory, reload
VCStack members take the release from the master automatically.
Roll back: boot system flash:/<old>.rel, write memory, reload.

AW+ has no MD5 command, so the copy is checked by its size on flash (SCP itself
checks the transfer). Confirm on the lab switch - see upgrade-lab-tests.md.
"""

import re

from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.drivers.base import Step, StepFailed, Unit, UpgradeDriver, UpgradePath

_ERR = re.compile(r"%\s*(Error|Invalid)|failed", re.I)


def file_size(listing: str, filename: str) -> int | None:
    """Size of a file in AW+ 'dir' output, e.g.
    '   45563908 -rw- Mar 10 2024 10:01:02  x930-5.5.4-1.1.rel'."""
    for line in (listing or "").splitlines():
        if line.rstrip().endswith(filename):
            m = re.search(r"^\s*(\d+)\s", line)
            if m:
                return int(m.group(1))
    return None


class AlliedAwplusDriver(UpgradeDriver):
    platform = "allied_awplus"
    default_fs = "flash:/"
    paths = [UpgradePath("standalone", "Standalone or VCStack", 1,
                         "boot system to the new release (old one as backup), reload")]

    def extra_commands(self, unit, image):
        return {"fw:dir": "dir flash:", "fw:scp": "show running-config ssh",
                "fw:stack": "show stack"}

    def extra_prechecks(self, ctx, unit: Unit, outputs, path):
        results = self.image_on_device_checks(ctx, unit, outputs)
        scp_out = outputs.get("fw:scp") or ""
        scp = "no ssh server scp" not in scp_out and (
            "ssh server scp" in scp_out or "service ssh" in scp_out)
        results.append(ck.CheckResult(
            "scp_server", "SCP server enabled (for the image copy)", ck.BLOCKER,
            ck.PASS if scp else ck.FAIL, "enabled" if scp else "not enabled",
            "" if scp else "Configure 'ssh server scp'"))
        old = unit.facts.get("image") or ""
        unit.running_image = f"flash:/{old}" if old and ":" not in old else old
        present = bool(old) and old.split("/")[-1] in (outputs.get("fw:dir") or "")
        results.append(ck.CheckResult(
            "previous_image", "Current release stays on flash (for roll back)", ck.BLOCKER,
            ck.PASS if present else ck.FAIL, unit.running_image or "unknown",
            "" if present else "The running release isn't on flash"))
        return results

    def verify(self, ctx, u, record=False):
        listing = ctx.show(u, [f"dir {self.file_system(u)}{ctx.image.filename}"])[0]
        size = file_size(listing, ctx.image.filename)
        ok = size == ctx.image.size
        if record:
            ctx.record_checks(u, "stage", [ck.CheckResult(
                "device_size", "Release on the device has the right size", ck.BLOCKER,
                ck.PASS if ok else ck.FAIL, f"{size} bytes" if size is not None else "not found",
                "" if ok else f"expected {ctx.image.size} bytes")])
        return ok

    def activate_steps(self, ctx, u):
        d = u.device_id
        return [Step(f"boot:{d}", "set_boot", f"{u.name}: boot system to the new release, "
                     "write memory", d),
                Step(f"reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    def rollback_steps(self, ctx, u):
        d = u.device_id
        return [Step(f"rb_boot:{d}", "set_boot", f"{u.name}: boot system to the previous release",
                     d, args={"previous": True}),
                Step(f"rb_reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    def step_set_boot(self, ctx, step, u):
        new = f"{self.file_system(u)}{ctx.image.filename}"
        old = u.running_image
        lines = [f"boot system {old}"] if step.args.get("previous") else \
            [f"boot system {new}"] + ([f"boot system backup {old}"] if old else [])
        ctx.change(u, lines, config=True)
        out = ctx.change(u, ["write memory"])
        if not ctx.dry_run and _ERR.search(out):
            raise StepFailed(f"write memory failed on {u.name}: {out.strip()[-300:]}")

    def step_reload(self, ctx, step, u):
        u.status = "activated"
        ctx.change(u, ["reload"], expect_reload=True)
