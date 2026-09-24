"""Admin command line.

  python -m app.cli hash-password            # for NETOPS_LOCAL_ADMIN_PASSWORD_HASH
  python -m app.cli import-csv devices.csv   # bulk-load devices
  python -m app.cli backup <device-name>     # run one backup now and print the result
  python -m app.cli firmware-check <device-name> [--raw]
                                             # read one device's version and show what was parsed

CSV columns: name,address,platform,site,credential,frequency_minutes,notes
(`credential` is the name of an existing credential profile; `site`,
`frequency_minutes` and `notes` are optional).
"""

import argparse
import csv
import getpass
import sys

from sqlalchemy import select


def _hash_password(_args) -> int:
    from app.core.crypto import hash_password

    pw = getpass.getpass("New local admin password: ")
    if len(pw) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if pw != getpass.getpass("Repeat: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print(f"NETOPS_LOCAL_ADMIN_PASSWORD_HASH='{hash_password(pw)}'")
    return 0


def _import_csv(args) -> int:
    from app.core.audit import audit
    from app.core.config import get_settings
    from app.core.db import init_engine, session_scope
    from app.core.inventory import DeviceIn
    from app.core.models import Credential, Device
    from app.tools.config_backup.service import ensure_states
    from app.tools.config_backup.models import BackupState

    settings = get_settings()
    init_engine(settings.database_url)
    created = skipped = 0
    with open(args.file, newline="", encoding="utf-8-sig") as fh, session_scope() as db:
        creds = {c.name: c.id for c in db.scalars(select(Credential))}
        existing = set(db.scalars(select(Device.name)))
        for line_no, row in enumerate(csv.DictReader(fh), start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            try:
                cred_name = row.get("credential", "")
                if cred_name and cred_name not in creds:
                    raise ValueError(f"unknown credential '{cred_name}'")
                data = DeviceIn(name=row.get("name", ""), address=row.get("address", ""),
                                platform=row.get("platform", ""), site=row.get("site", ""),
                                credential_id=creds.get(cred_name), notes=row.get("notes", ""))
                if data.name in existing:
                    print(f"line {line_no}: {data.name} already exists, skipped")
                    skipped += 1
                    continue
                device = Device(**data.model_dump())
                db.add(device)
                db.flush()
                freq = int(row.get("frequency_minutes") or settings.default_frequency_minutes)
                db.add(BackupState(device_id=device.id, frequency_minutes=max(15, freq)))
                existing.add(data.name)
                created += 1
            except Exception as exc:  # noqa: BLE001
                print(f"line {line_no}: {exc}", file=sys.stderr)
                skipped += 1
        ensure_states(db, settings.default_frequency_minutes)
        audit(db, getpass.getuser(), "device.import_csv", f"{created} created from {args.file}")
    print(f"Imported {created} device(s), skipped {skipped}.")
    return 0


def _backup(args) -> int:
    from app.core.config import get_settings
    from app.core.crypto import CredentialCipher
    from app.core.db import init_engine, session_scope
    from app.core.models import Device
    from app.tools.config_backup.models import BackupRun
    from app.tools.config_backup.service import BackupService
    from app.tools.config_backup.storage import GitConfigStore

    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as db:
        device = db.scalar(select(Device).where(Device.name == args.name))
        if device is None:
            print(f"No device named {args.name}", file=sys.stderr)
            return 1
        device_id = device.id
    svc = BackupService(settings, CredentialCipher(settings.credential_key_file),
                        GitConfigStore(settings.configs_dir))
    run_id = svc.run(device_id, f"cli:{getpass.getuser()}")
    with session_scope() as db:
        run = db.get(BackupRun, run_id)
        print(f"{run.status}: {run.message}")
        return 0 if run.status == "success" else 2


def _firmware_check(args) -> int:
    from app.core.config import get_settings
    from app.core.crypto import CredentialCipher
    from app.core.db import init_engine, session_scope
    from app.core.models import Device
    from app.core.ssh import classify_error, run_commands, target_for
    from app.tools.firmware_upgrade.facts import FACT_COMMANDS, parse_facts

    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as db:
        device = db.scalar(select(Device).where(Device.name == args.name))
        if device is None:
            print(f"No device named {args.name}", file=sys.stderr)
            return 1
        platform = device.platform
        try:
            target = target_for(device, CredentialCipher(settings.credential_key_file),
                                settings.ssh_timeout, settings.command_timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"failed: {classify_error(exc)[1]}", file=sys.stderr)
            return 2
    commands = FACT_COMMANDS.get(platform)
    if commands is None:
        print(f"Version checks are not supported for {platform}", file=sys.stderr)
        return 1
    try:
        outputs = run_commands(target, commands)
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {classify_error(exc)[1]}", file=sys.stderr)
        return 2
    if args.raw:
        for cmd, out in zip(commands, outputs):
            print(f"===== {cmd} =====\n{out}\n")
    try:
        facts = parse_facts(platform, outputs)
    except ValueError as exc:
        print(f"failed: {exc} (run with --raw to see the output)", file=sys.stderr)
        return 2
    for key, value in vars(facts).items():
        print(f"{key:12} {value if value is not None else '-'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash-password").set_defaults(func=_hash_password)
    p = sub.add_parser("import-csv")
    p.add_argument("file")
    p.set_defaults(func=_import_csv)
    p = sub.add_parser("backup")
    p.add_argument("name")
    p.set_defaults(func=_backup)
    p = sub.add_parser("firmware-check")
    p.add_argument("name")
    p.add_argument("--raw", action="store_true", help="also print the raw command output")
    p.set_defaults(func=_firmware_check)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
