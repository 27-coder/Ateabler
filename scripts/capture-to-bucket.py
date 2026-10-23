"""Explorer launch entry point; only URL metadata is read locally."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from ateable.capture_pc import main

raise SystemExit(main())
