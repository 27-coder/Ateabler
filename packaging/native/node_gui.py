"""Windowed participant entry point with a credential file picker."""

from native_smoke import maybe_smoke

if __name__ == "__main__":
    maybe_smoke()
    from ateable.participant_ui import main

    raise SystemExit(main())
