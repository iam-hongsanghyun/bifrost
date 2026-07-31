"""Put the repo root on sys.path so tests can `from bifrost...`.

Bifrost's package is `bifrost/`, deliberately NOT `backend/app/` — Ragnarok uses
that path, and two checkouts sharing a package name cannot be imported into one
process. The integration test needs exactly that, so the collision had to go.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
