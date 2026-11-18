"""Explicit offline build of the Android TV repair; optional checksum-pinned downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import VERSIONS, PatchError, build, pins


def main() -> int:
    parser = argparse.ArgumentParser(prog="python3 -m oh_androidtv_patch", description=__doc__)
    parser.add_argument("--versions", type=Path, default=VERSIONS,
                        help="versions.json whose openHAB pin must equal the repair's (default: the application's)")
    parser.add_argument("--java", type=Path, required=True, help="Java21 executable (a JRE is sufficient)")
    parser.add_argument("--runtime", type=Path, required=True, help="pinned unpacked runtime directory")
    parser.add_argument("--work", type=Path, required=True, help="owned scratch build/input cache")
    parser.add_argument("--output", type=Path, required=True, help="owned directory for the bundle and its build report")
    parser.add_argument("--fetch", action="store_true", help="download only the five fixed checksum-pinned public inputs")
    parser.add_argument("--test-sources", type=Path, help="tests/androidtv_patch for actual patched-manager offline tests")
    parser.add_argument("--write-classes", action="store_true",
                        help="record this build as the reviewed one: classes/ and their pins in inputs.json")
    args = parser.parse_args()
    try:
        args.work.mkdir(parents=True, exist_ok=True)
        if args.fetch:
            from oh_sandbox import download
            inputs = {key: download(entry["url"], entry["sha256"], args.work, log=lambda _: None)
                      for key, entry in pins()["assets"].items()}
        else:
            inputs = {key: args.work / entry["name"] for key, entry in pins()["assets"].items()}
        report = build(inputs, java=args.java, runtime=args.runtime, work=args.work,
                       output=args.output, test_sources=args.test_sources, versions=args.versions,
                       write_classes=args.write_classes)
        print(json.dumps({"artifact": report["artifact"], "activation": report["activation"]}, indent=2))
        return 0
    except (PatchError, OSError, KeyError, ValueError) as exc:
        parser.exit(2, f"Android TV patch: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
