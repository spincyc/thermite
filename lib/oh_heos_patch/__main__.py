"""Explicit offline HEOS patch build, and owner activation inputs; optional checksum-pinned downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import VERSIONS, PatchError, build, pins, prepare


def main() -> int:
    parser = argparse.ArgumentParser(prog="python3 -m oh_heos_patch", description=__doc__)
    parser.add_argument("--versions", type=Path, default=VERSIONS,
                        help="versions.json whose openHAB pin must equal the patch's (default: the application's)")
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("build", help="rebuild the pinned bundle with the reviewed patch")
    make.add_argument("--java", type=Path, required=True, help="Java21 executable (a JRE is sufficient)")
    make.add_argument("--runtime", type=Path, required=True, help="pinned unpacked runtime directory")
    make.add_argument("--work", type=Path, required=True, help="owned scratch build/input cache")
    make.add_argument("--output", type=Path, required=True, help="owned staging directory; never a live userdata directory")
    make.add_argument("--fetch", action="store_true", help="download only the fixed checksum-pinned public inputs")
    make.add_argument("--test-sources", type=Path, help="tests/heos_patch: reconnect gate and its negative control")
    stage = sub.add_parser("prepare", help="owner activation files from copies of the runtime's Karaf files")
    stage.add_argument("--staged", type=Path, required=True, help="the build --output directory")
    stage.add_argument("--maven", type=Path, required=True, help="copy of userdata/etc/org.ops4j.pax.url.mvn.cfg")
    stage.add_argument("--overrides", type=Path, help="copy of userdata/etc/overrides.properties, when it exists")
    stage.add_argument("--output", type=Path, required=True, help="fresh private directory for the activation files")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.staged, maven=args.maven, overrides=args.overrides, output=args.output,
                             versions=args.versions)
            print(json.dumps(result, indent=2))
            return 0
        args.work.mkdir(parents=True, exist_ok=True)
        if args.fetch:
            from oh_sandbox import download
            inputs = {key: download(entry["url"], entry["sha256"], args.work, log=lambda _: None)
                      for key, entry in pins()["assets"].items()}
        else:
            inputs = {key: args.work / entry["name"] for key, entry in pins()["assets"].items()}
        report = build(inputs, java=args.java, runtime=args.runtime, work=args.work, output=args.output,
                       test_sources=args.test_sources, versions=args.versions)
        print(json.dumps({"artifact": report["artifact"], "classes": report["classes"],
                          "java_tests": [line for line in report.get("java_tests", "").splitlines() if line.startswith("PASS")] or "not run",
                          "negative_control": report.get("negative_control", "not run"),
                          "activation": report["activation"]}, indent=2))
        return 0
    except (PatchError, OSError, KeyError, ValueError) as exc:
        parser.exit(2, f"HEOS patch: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
