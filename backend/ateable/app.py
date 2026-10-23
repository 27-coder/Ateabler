"""Unified native entry point for the Ateable desktop app and CLI."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Open the desktop with no arguments; otherwise dispatch to the CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    # Finder may add this legacy process-serial-number argument to macOS apps.
    args = [value for value in args if not value.startswith("-psn_")]
    if args:
        from ateable.cli import main as cli_main

        return cli_main(args)

    from ateable.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
