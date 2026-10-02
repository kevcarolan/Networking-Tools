"""Cisco IOS / IOS-XE.

Install mode (IOS-XE, standalone or stack):
    install add file flash:<image>                       (no reload)
    install activate auto-abort-timer <N> prompt-level none   (reloads)
    ... post-checks ...
    install commit            only after the post-checks pass (or are overridden)
    Roll back before the commit: install abort   (the switch also reverts by itself
    when the abort timer runs out). After the commit: install rollback to id <id>,
    the rollback point recorded at the pre-check.

Bundle mode / classic IOS:
    boot system flash:<new>, boot system <old> (fallback), write memory, reload
    Roll back: boot system <old>, write memory, reload
"""

import re

from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.drivers.base import Step, StepFailed, Unit, UpgradeDriver, UpgradePath

_FAILED = re.compile(r"FAILED|%\s*Error|Error:|aborted", re.I)


class CiscoIosDriver(UpgradeDriver):
    platform = "cisco_ios"
    default_fs = "flash:"
    paths = [
        UpgradePath("install", "IOS-XE install mode (standalone or stack)", 1,
                    "install add / activate with an auto-abort timer; committed only after "
                    "the post-checks pass"),
        UpgradePath("bundle", "Bundle mode / classic IOS", 1,
                    "boot system to the new image, keep the old one as fallback, reload"),
    ]

    def default_path(self, facts: dict) -> str:
        return "install" if facts.get("boot_mode") == "install" else "bundle"

    def extra_commands(self, unit, image):
        return {"fw:scp": "show running-config | include ip scp server",
                "fw:boot": "show running-config | include ^boot system",
                "fw:rollback": "show install rollback"}

    def extra_prechecks(self, ctx, unit: Unit, outputs, path):
        results = self.image_on_device_checks(ctx, unit, outputs)
        scp = outputs.get("fw:scp") or ""
        results.append(ck.CheckResult(
            "scp_server", "SCP server enabled (for the image copy)", ck.BLOCKER,
            ck.PASS if "ip scp server enable" in scp else ck.FAIL,
            "enabled" if "ip scp server enable" in scp else "not enabled",
            "" if "ip scp server enable" in scp else
            "Configure 'ip scp server enable' (the upgrade account also needs privilege 15 "
            "through AAA exec authorization)"))
        unit.running_image = unit.facts.get("image") or ""
        if path.id == "install":
            mode = unit.facts.get("boot_mode")
            results.append(ck.CheckResult(
                "install_mode", "Device runs in install mode", ck.BLOCKER,
                ck.PASS if mode == "install" else ck.FAIL, mode or "unknown",
                "" if mode == "install" else "Use the bundle-mode path, or convert the switch "
                                              "to install mode first"))
            ids = re.findall(r"^\s*(\d+)\s+", outputs.get("fw:rollback") or "", re.M)
            unit.rollback_ref = max(ids, key=int) if ids else ""
            results.append(ck.CheckResult(
                "rollback_point", "Rollback point recorded", ck.INFO,
                ck.PASS if unit.rollback_ref else ck.SKIP,
                f"install rollback id {unit.rollback_ref}" if unit.rollback_ref else "none listed",
                "" if unit.rollback_ref else
                "Before the commit, Roll back uses 'install abort'; after it there is no "
                "rollback point"))
        else:
            mode = unit.facts.get("boot_mode")
            results.append(ck.CheckResult(
                "bundle_mode", "Device boots a single image file", ck.BLOCKER,
                ck.PASS if mode != "install" else ck.FAIL, mode or "bundle / classic",
                "" if mode != "install" else "This switch runs in install mode: use the "
                                              "install-mode path"))
            old = unit.running_image
            listing = outputs.get("dir") or ""
            present = bool(old) and old.split(":")[-1].lstrip("/") in listing
            results.append(ck.CheckResult(
                "previous_image", "Current image stays on flash (for roll back)", ck.BLOCKER,
                ck.PASS if present else ck.FAIL, old or "unknown",
                "" if present else "The running image isn't on flash, so a roll back would "
                                   "have nothing to boot"))
        return results

    # --- the procedure -----------------------------------------------------------

    def activate_steps(self, ctx, u: Unit) -> list[Step]:
        d = u.device_id
        if ctx.path.id == "install":
            return [Step(f"install_add:{d}", "install_add", f"{u.name}: install add (no reload)", d),
                    Step(f"activate:{d}", "install_activate",
                         f"{u.name}: install activate (reloads; auto-abort timer "
                         f"{ctx.settings().upgrade_abort_timer_min} min)", d, reload=True)]
        return [Step(f"boot:{d}", "set_boot", f"{u.name}: boot system to the new image, "
                     "write memory", d),
                Step(f"reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    def finish_steps(self, ctx, u: Unit) -> list[Step]:
        if ctx.path.id == "install":
            return [Step(f"commit:{u.device_id}", "install_commit", f"{u.name}: install commit",
                         u.device_id)]
        return []

    def rollback_steps(self, ctx, u: Unit) -> list[Step]:
        d = u.device_id
        if ctx.path.id == "install":
            return [Step(f"rb:{d}", "install_rollback", f"{u.name}: install abort / rollback "
                         "to the previous version (reloads)", d, reload=True)]
        return [Step(f"rb_boot:{d}", "set_boot", f"{u.name}: boot system to the previous image",
                     d, args={"previous": True}),
                Step(f"rb_reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    # --- steps ---------------------------------------------------------------------

    def step_install_add(self, ctx, step, u):
        out = ctx.change(u, [f"install add file {self.file_system(u)}{ctx.image.filename}"],
                         long=True)
        if not ctx.dry_run and (_FAILED.search(out) or "SUCCESS" not in out.upper()):
            raise StepFailed(f"install add failed on {u.name}: {out.strip()[-400:]}")

    def step_install_activate(self, ctx, step, u):
        minutes = ctx.settings().upgrade_abort_timer_min
        u.status = "activated"
        out = ctx.change(u, [f"install activate auto-abort-timer {minutes} prompt-level none"],
                         expect_reload=True, long=True)
        if not ctx.dry_run and _FAILED.search(out.split("[connection closed")[0]):
            raise StepFailed(f"install activate failed on {u.name}: {out.strip()[-400:]}",
                             attention=True)
        ctx.set_job(pending_commit=True, abort_minutes=minutes)

    def step_install_commit(self, ctx, step, u):
        out = ctx.change(u, ["install commit"], long=True)
        if not ctx.dry_run and _FAILED.search(out):
            raise StepFailed(f"install commit failed on {u.name}: {out.strip()[-400:]}",
                             attention=True)
        ctx.set_job(pending_commit=False)

    def step_install_rollback(self, ctx, step, u):
        if ctx.pending_commit:
            cmd = "install abort prompt-level none"
        elif u.rollback_ref:
            cmd = f"install rollback to id {u.rollback_ref} prompt-level none"
        else:
            raise StepFailed(f"No rollback point was recorded for {u.name} and the new version "
                             "is already committed: roll back by hand", attention=True)
        out = ctx.change(u, [cmd], expect_reload=True, long=True)
        if not ctx.dry_run and _FAILED.search(out.split("[connection closed")[0]):
            raise StepFailed(f"'{cmd}' failed on {u.name}: {out.strip()[-400:]}", attention=True)
        ctx.set_job(pending_commit=False)

    def step_set_boot(self, ctx, step, u):
        new = f"{self.file_system(u)}{ctx.image.filename}"
        old = u.running_image
        lines = ["no boot system"]
        if step.args.get("previous"):
            lines.append(f"boot system {old}")
        else:
            lines += [f"boot system {new}"] + ([f"boot system {old}"] if old else [])
        ctx.change(u, lines, config=True)
        out = ctx.change(u, ["write memory"])
        if not ctx.dry_run and _FAILED.search(out):
            raise StepFailed(f"write memory failed on {u.name}: {out.strip()[-300:]}")

    def step_reload(self, ctx, step, u):
        u.status = "activated"
        ctx.change(u, ["reload"], expect_reload=True)
