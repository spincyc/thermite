"""Build thermite's docs site, check it, preview it, and export it to the
thermite repository.

    thermite docs build --out DIR
    thermite docs serve [--port PORT]
    thermite docs check [--to DIR]
    thermite docs export --to DIR [--dry-run]
"""

from __future__ import annotations

import http.server
import mimetypes
import sys
from pathlib import Path

import oh_site as site
from oh_cli import action, actions, add_dry_run, command

DESCRIPTION = """\
The docs site is plain HTML, made from site/: its pages, every verb's own
help (the commands page), and the torch and color themes. The thermite
repository publishes it with GitHub Pages, so it is public: every action
first refuses a site that names this house: an address, a MAC, the host's
name, state root or time zone, a device's id, name, model or make, a room's
label, or a word in site/private-words.txt. The house-only verbs are left out.
"""

NOTES = """\
export writes docs/ (the site), README.md, FAQ.md and the Pages workflow
into a checkout of the thermite repository, and removes files under docs/
that the site no longer has. Commit and push there to publish.

--with-code also exports the reviewed portable source and a fictional
sample house. It checks privacy and runs offline configuration, help,
theme, docs and lint checks before writing any destination file. It starts
no device connection or native runtime. Only previously managed source
files can be removed; unrelated files and license terms stay untouched.
"""


def built() -> tuple[dict[str, bytes], list[str]]:
    files = site.build()
    return files, site.guard({**files, **{name: (site.SOURCE / name).read_bytes() for name in site.TOP}},
                             site.house_facts())


def refuse(problems: list[str]) -> int:
    print("thermite docs: the site names this house; fix site/ or the help that says it:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1


def cmd_build(args) -> int:
    files, problems = built()
    if problems:
        return refuse(problems)
    for path, data in files.items():
        target = args.out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"wrote {len(files)} files to {args.out}; open {args.out / 'index.html'}")
    return 0


def cmd_serve(args) -> int:
    files, problems = built()
    if problems:
        return refuse(problems)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0].lstrip("/") or "index.html"
            if path.endswith("/"):
                path += "index.html"
            data = files.get(path)
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"the site is at http://127.0.0.1:{server.server_port}/ (Ctrl-C stops it)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_check(args) -> int:
    files, problems = built()
    if problems:
        return refuse(problems)
    print(f"the site builds: {len(files)} files, none naming this house")
    if getattr(args, "with_code", False):
        import oh_public
        try:
            files = oh_public.bundle(site.repository_files())
            oh_public.smoke(files)
            print(f"portable source checks passed: {len(files)} managed files, no device or runtime calls")
            stale, stray = oh_public.compare(args.to, files) if args.to is not None else ([], [])
        except (oh_public.PublicError, OSError, ValueError) as exc:
            print(f"thermite docs: {exc}", file=sys.stderr)
            return 1
        if args.to is None:
            return 0
    else:
        if args.to is None:
            return 0
        stale, stray = site.compare(args.to, site.repository_files())
    if not stale and not stray:
        print(f"{args.to} is up to date")
        return 0
    for path in stale:
        print(f"stale: {path}")
    for path in stray:
        print(f"stray: {path}")
    print(f"{args.to} is out of date: thermite docs export --to {args.to}", file=sys.stderr)
    return 1


def cmd_export(args) -> int:
    if not args.to.is_dir():
        print(f"thermite docs: {args.to} is not a directory (a checkout of the thermite repository)",
              file=sys.stderr)
        return 2
    _, problems = built()
    if problems:
        return refuse(problems)
    files = site.repository_files()
    if getattr(args, "with_code", False):
        import oh_public
        try:
            files = oh_public.bundle(files)
            oh_public.smoke(files)
            stale, stray = oh_public.compare(args.to, files) if args.dry_run else oh_public.write(args.to, files)
        except (oh_public.PublicError, OSError, ValueError) as exc:
            print(f"thermite docs: {exc}", file=sys.stderr)
            return 1
    else:
        stale, stray = site.compare(args.to, files) if args.dry_run else site.write(args.to, files)
    verb = ("would write", "would remove") if args.dry_run else ("wrote", "removed")
    for path in stale:
        print(f"{verb[0]} {path}")
    for path in stray:
        print(f"{verb[1]} {path}")
    if not stale and not stray:
        print(f"{args.to} is already up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = command("docs", DESCRIPTION, (
        ("thermite docs serve", "preview the site at http://127.0.0.1:8000/"),
        ("thermite docs check", "does it build, without naming this house?"),
        ("thermite docs export --to ../thermite", "update the thermite repository's copy"),
    ), notes=NOTES)
    sub = actions(parser)

    build = action(sub, "build", "write the site into a directory", examples=(
        ("thermite docs build --out /tmp/thermite-site", "then open /tmp/thermite-site/index.html"),
    ))
    build.add_argument("--out", type=Path, required=True, metavar="DIR", help="where to write it")
    build.set_defaults(func=cmd_build)

    serve = action(sub, "serve", "preview the site on 127.0.0.1, without writing it anywhere", examples=(
        ("thermite docs serve", "at http://127.0.0.1:8000/"),
        ("thermite docs serve --port 0", "on any free port; it prints which"),
    ))
    serve.add_argument("--port", type=int, default=8000, help="the port (default: 8000)")
    serve.set_defaults(func=cmd_serve)

    check = action(sub, "check", "build the site in memory and refuse anything naming this house", examples=(
        ("thermite docs check", "the site alone"),
        ("thermite docs check --to ../thermite", "and exit 1 if that checkout's copy is stale"),
    ))
    check.add_argument("--to", type=Path, metavar="DIR", help="a checkout of the thermite repository to compare")
    check.add_argument("--with-code", action="store_true", help="also check the portable source and fictional sample")
    check.set_defaults(func=cmd_check)

    export = action(sub, "export", "write the site, README.md and FAQ.md into the thermite repository", examples=(
        ("thermite docs export --to ../thermite --dry-run", "what it would write and remove"),
        ("thermite docs export --to ../thermite", "write them; then commit and push there"),
    ))
    export.add_argument("--to", type=Path, required=True, metavar="DIR",
                        help="a checkout of the thermite repository")
    export.add_argument("--with-code", action="store_true", help="include checked portable source and a fictional sample house")
    add_dry_run(export)
    export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)
