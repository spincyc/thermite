"""Run only from a verified root-owned program package: python3 -I -S -B owner_run.py."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def check_owner(path):
    for value in (path, *path.parents):
        info = value.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("root_owned_nonwritable_path_required")


def validate(program, expected):
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ValueError("external_program_sha_required")
    check_owner(program)
    receipt = program / "program.json"
    check_owner(receipt)
    info = receipt.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 32768:
        raise ValueError("program_receipt_not_regular_bounded")
    data = receipt.read_bytes()
    if len(data) > 32768 or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("external_program_sha_mismatch")
    value = json.loads(data)
    package = program / "oh_androidtv_patch"
    check_owner(package)
    if (value.get("version") != 1 or not isinstance(value.get("files"), dict)
            or not 5 <= len(value["files"]) <= 32 or set(value["files"]) != {p.name for p in package.iterdir()}):
        raise ValueError("program_scope_mismatch")
    for name, digest in value["files"].items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("program_entry_invalid")
        path = package / name
        check_owner(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 131072:
            raise ValueError("program_entry_not_regular_bounded")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("program_code_hash_mismatch")
    return package


def main():
    try:
        if os.geteuid() != 0 or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise ValueError("owner_root_isolated_python_required")
        if len(sys.argv) < 4 or sys.argv[1] != "--program-sha256" or sys.argv[3] not in ("apply", "rollback"):
            raise ValueError("fixed_apply_or_rollback_required")
        program = Path(__file__).absolute().parents[1]
        validate(program, sys.argv[2])
        sys.path.insert(0, str(program))
        from oh_androidtv_patch.maintenance import main as maintenance_main
        return maintenance_main(sys.argv[3:])
    except (OSError, ValueError) as error:
        reason = str(error) if re.fullmatch(r"[a-z0-9_]+", str(error)) else "owner_launcher_refused"
        print(json.dumps({"status": "refused", "reason": reason}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
