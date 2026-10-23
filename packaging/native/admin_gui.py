"""Windowed admin entry point; shell capture has no console on Windows."""

from native_smoke import maybe_smoke

if __name__ == "__main__":
    maybe_smoke()
    from ateable.desktop import main

    raise SystemExit(main())
