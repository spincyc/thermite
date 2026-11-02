"""Explicit offline build; optional checksum-pinned download through oh_sandbox."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import PatchError, build, pins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", type=Path, required=True, help="Java21 executable (a JRE is sufficient)")
    parser.add_argument("--runtime", type=Path, required=True, help="pinned unpacked runtime directory")
    parser.add_argument("--work", type=Path, required=True, help="owned scratch build/input cache")
    parser.add_argument("--output", type=Path, required=True, help="owned staging directory; never a live addons directory")
    parser.add_argument("--fetch", action="store_true", help="download only the five fixed checksum-pinned public inputs")
    parser.add_argument("--test-sources", type=Path, help="tests/androidtv_patch for actual patched-manager offline tests")
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
                       output=args.output, test_sources=args.test_sources)
        print(json.dumps({"artifact": report["artifact"], "activation": report["activation"]}, indent=2))
        return 0
    except (PatchError, OSError, KeyError) as exc:
        parser.exit(2, f"Android TV patch: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
