"""Cisco ASA: standalone, or an active/standby failover pair.

Failover pair: both units are NetOps devices - one is the active address, the other
the standby address (the device's peer). The addresses follow the roles, so after a
failover the "active" device reaches the other physical unit. The procedure:

  1. copy and verify the image on both units
  2. boot system on the active unit (it replicates), write memory on both
  3. failover reload-standby          -> the standby unit boots the new image
  4. wait until it is Standby Ready, post-check it
  5. failover active on the standby   -> the upgraded unit takes over
  6. failover reload-standby          -> the other unit boots the new image
  7. wait until it is Standby Ready, post-check both
"""

import re

from app.tools.firmware_upgrade import checks as ck
from app.tools.firmware_upgrade.drivers.base import Step, StepFailed, Unit, UpgradeDriver, UpgradePath

_ERR = re.compile(r"ERROR|%\s*Error|failed", re.I)


def _role(failover_out: str) -> str:
    fo = ck.parse_failover(failover_out or "")
    if not fo.get("enabled"):
        return "standalone"
    state = fo.get("this", "").lower()
    return "active" if state == "active" else "standby" if "standby" in state else state


class CiscoAsaDriver(UpgradeDriver):
    platform = "cisco_asa"
    default_fs = "disk0:/"
    paths = [
        UpgradePath("standalone", "Standalone ASA", 1, "boot system, write memory, reload"),
        UpgradePath("failover_pair", "Failover pair (standby first, then fail over)", 2,
                    "Zero-downtime: reload the standby, make it active, then reload the other unit"),
    ]

    def extra_commands(self, unit, image):
        return {"fw:scp": "show running-config ssh", "fw:boot": "show running-config boot"}

    def extra_prechecks(self, ctx, unit: Unit, outputs, path):
        results = self.image_on_device_checks(ctx, unit, outputs)
        scp = "ssh scopy enable" in (outputs.get("fw:scp") or "")
        results.append(ck.CheckResult(
            "scp_server", "SCP server enabled (for the image copy)", ck.BLOCKER,
            ck.PASS if scp else ck.FAIL, "enabled" if scp else "not enabled",
            "" if scp else "Configure 'ssh scopy enable'"))
        unit.running_image = unit.facts.get("image") or ""
        old = unit.running_image.split(":")[-1].lstrip("/")
        present = bool(old) and old in (outputs.get("dir") or "")
        results.append(ck.CheckResult(
            "previous_image", "Current image stays on disk0 (for roll back)", ck.BLOCKER,
            ck.PASS if present else ck.FAIL, unit.running_image or "unknown",
            "" if present else "The running image isn't on disk0"))
        unit.role = _role(outputs.get("failover"))
        if path.units == 1 and unit.role not in ("standalone", ""):
            results.append(ck.CheckResult(
                "standalone", "Not part of a failover pair", ck.BLOCKER, ck.FAIL, unit.role,
                "This ASA is in a failover pair: use the failover-pair path so the units are "
                "upgraded one at a time"))
        return results

    def pair_checks(self, ctx, units, path):
        roles = sorted(u.role for u in units)
        ok = roles == ["active", "standby"]
        return [ck.CheckResult("pair_roles", "One active and one standby unit", ck.BLOCKER,
                               ck.PASS if ok else ck.FAIL,
                               ", ".join(f"{u.name}: {u.role or '?'}" for u in units),
                               "" if ok else "Set the job's device and its peer to the pair's "
                                             "active and standby addresses")]

    def order_units(self, units):
        return sorted(units, key=lambda u: 0 if u.role == "standby" else 1)

    # --- plans ---------------------------------------------------------------------

    def upgrade_plan(self, ctx, path, units):
        if path.units == 1:
            return self.unit_steps(ctx, units[0])
        s, a = units  # standby first
        return [
            Step(f"backup_before:{a.device_id}", "backup", f"{a.name}: config backup before",
                 a.device_id, args={"label": "before"}),
            Step(f"stage:{s.device_id}", "stage", f"{s.name}: copy the image and check its MD5",
                 s.device_id),
            Step(f"stage:{a.device_id}", "stage", f"{a.name}: copy the image and check its MD5",
                 a.device_id),
            Step(f"boot:{a.device_id}", "set_boot", "Active unit: boot system to the new image, "
                 "write memory on both units", a.device_id),
            *self._standby_cycle("1", a, s, f"{s.name}: reload the standby unit"),
            Step(f"postcheck:{s.device_id}", "postcheck", f"{s.name}: post-checks", s.device_id,
                 gate=True),
            Step(f"make_active:{s.device_id}", "make_active", f"{s.name}: failover active "
                 "(the upgraded unit takes over)", s.device_id, reload=True),
            Step(f"ready_after_swap:{a.device_id}", "failover_ready", "Failover pair settled",
                 a.device_id),
            *self._standby_cycle("2", a, s, "Reload the other unit (now standby)"),
            Step(f"postcheck2:{a.device_id}", "postcheck", f"{a.name}: post-checks", a.device_id,
                 gate=True),
            Step(f"postcheck2:{s.device_id}", "postcheck", f"{s.name}: post-checks", s.device_id,
                 gate=True),
            Step(f"backup_after:{a.device_id}", "backup", f"{a.name}: config backup after",
                 a.device_id, args={"label": "after"}),
        ]

    def _standby_cycle(self, n, a: Unit, s: Unit, label: str, previous=False) -> list[Step]:
        return [
            Step(f"reload_standby{n}:{a.device_id}", "reload_standby", label, a.device_id,
                 reload=True, args={"standby": s.device_id}),
            Step(f"wait{n}:{s.device_id}", "wait", f"{s.name}: wait for the standby unit",
                 s.device_id),
            Step(f"ready{n}:{a.device_id}", "failover_ready", "Wait for Standby Ready",
                 a.device_id),
        ]

    def rollback_plan(self, ctx, path, units):
        if path.units == 1:
            return super().rollback_plan(ctx, path, units)
        s, a = self.order_units(units)
        return [
            Step(f"rb_boot:{a.device_id}", "set_boot", "Active unit: boot system to the previous "
                 "image, write memory on both units", a.device_id, args={"previous": True}),
            *self._standby_cycle("rb1", a, s, f"{s.name}: reload the standby unit"),
            Step(f"rb_make_active:{s.device_id}", "make_active", f"{s.name}: failover active",
                 s.device_id, reload=True),
            Step(f"rb_ready:{a.device_id}", "failover_ready", "Failover pair settled", a.device_id),
            *self._standby_cycle("rb2", a, s, "Reload the other unit (now standby)"),
            Step(f"rb_postcheck:{a.device_id}", "postcheck", f"{a.name}: post-checks on the "
                 "previous version", a.device_id, gate=True, args={"target": "from"}),
            Step(f"rb_postcheck:{s.device_id}", "postcheck", f"{s.name}: post-checks on the "
                 "previous version", s.device_id, gate=True, args={"target": "from"}),
        ]

    def activate_steps(self, ctx, u):
        d = u.device_id
        return [Step(f"boot:{d}", "set_boot", f"{u.name}: boot system to the new image, "
                     "write memory", d),
                Step(f"reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    def rollback_steps(self, ctx, u):
        d = u.device_id
        return [Step(f"rb_boot:{d}", "set_boot", f"{u.name}: boot system to the previous image",
                     d, args={"previous": True}),
                Step(f"rb_reload:{d}", "reload", f"{u.name}: reload", d, reload=True)]

    # --- steps ---------------------------------------------------------------------

    def step_set_boot(self, ctx, step, u):
        new = f"{self.file_system(u)}{ctx.image.filename}"
        old = u.running_image
        lines = ["clear configure boot system"]
        lines += [f"boot system {old}"] if step.args.get("previous") else \
            [f"boot system {new}"] + ([f"boot system {old}"] if old else [])
        ctx.change(u, lines, config=True)
        cmds = ["write memory"] + (["failover exec mate write memory"]
                                   if ctx.path.units == 2 else [])
        out = ctx.change(u, cmds)
        if not ctx.dry_run and _ERR.search(out):
            raise StepFailed(f"write memory failed: {out.strip()[-300:]}")

    def step_reload(self, ctx, step, u):
        u.status = "activated"
        ctx.change(u, ["reload noconfirm"], expect_reload=True)

    def step_reload_standby(self, ctx, step, u):
        standby = ctx.unit(step.args["standby"])
        standby.status = "activated"
        out = ctx.change(u, ["failover reload-standby"])
        if not ctx.dry_run and _ERR.search(out):
            raise StepFailed(f"failover reload-standby failed: {out.strip()[-300:]}",
                             attention=True)

    def step_make_active(self, ctx, step, u):
        # The session drops when this unit takes over the active address.
        # The addresses follow the roles, so the devices keep their roles (active /
        # standby address); only the physical units behind them swap.
        ctx.change(u, ["failover active"], expect_reload=True)

    def step_failover_ready(self, ctx, step, u):
        """The unit at the active address is Active and the other unit is Standby Ready."""
        if ctx.dry_run:
            ctx.event("DRY RUN - would wait until 'show failover' shows Active / Standby Ready",
                      step.kind, "action", u.device_id)
            return

        def ready():
            fo = ck.parse_failover(ctx.show(u, ["show failover"])[0])
            return fo.get("this", "").lower() == "active" and \
                fo.get("other", "").lower() == "standby ready"

        if not ctx.poll(ready, minutes=ctx.settings().upgrade_reload_timeout_min,
                        what="the failover pair to show Active / Standby Ready"):
            raise StepFailed("The failover pair didn't reach Active / Standby Ready in time",
                             attention=True)
