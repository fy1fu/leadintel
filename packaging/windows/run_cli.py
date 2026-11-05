"""Runs the LeadIntel command line from an installed Friend Edition (no package build or entry point needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.cli import main  # noqa: E402

raise SystemExit(main())
