#!/usr/bin/env python3
"""
docforge — documentation pipeline for fetch-forge.

Commands:
  site    Generate MkDocs content from db/*.yaml and build the site.
  serve   Generate + serve a live-reloading dev server (default port 9000).
  help    Show this help.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

def _bold(s):  return f"\033[1m{s}\033[0m"
def _cyan(s):  return f"\033[36m{s}\033[0m"
def _green(s): return f"\033[32m{s}\033[0m"
def _red(s):   return f"\033[31m{s}\033[0m"
def _dim(s):   return f"\033[2m{s}\033[0m"


def _generate():
    """Run generate_mkdocs to produce docs/ content."""
    from scripts import generate_mkdocs
    print(_cyan("Generating MkDocs content from db/..."))
    generate_mkdocs.generate()
    print(_green("Done."))


def cmd_site(args):
    """Build the full MkDocs site."""
    _generate()
    site_dir = ROOT / "mkdocs-site"
    print(_cyan("Building MkDocs site..."))
    subprocess.run(["mkdocs", "build"], cwd=site_dir, check=True)
    print(_green(f"Site built → {site_dir}/site/"))


def cmd_serve(args):
    """Generate content and start a live-reloading dev server (default port 9000)."""
    _generate()
    site_dir = ROOT / "mkdocs-site"
    port = "9000"
    for arg in args:
        if arg.startswith("--port="):
            port = arg.split("=", 1)[1]
        elif arg.isdigit():
            port = arg
    addr = f"127.0.0.1:{port}"
    print(_cyan(f"Starting MkDocs dev server on http://{addr} ..."))
    subprocess.run(
        ["mkdocs", "serve", "--dev-addr", addr],
        cwd=site_dir,
        check=True,
    )


def cmd_help(args):
    print(__doc__)


COMMANDS = {
    "site":  cmd_site,
    "serve": cmd_serve,
    "help":  cmd_help,
    "--help": cmd_help,
    "-h":    cmd_help,
}


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "help"
    args = argv[1:]

    fn = COMMANDS.get(cmd)
    if fn is None:
        print(_red(f"error: unknown command '{cmd}'"))
        cmd_help([])
        sys.exit(1)

    fn(args)


if __name__ == "__main__":
    main()
