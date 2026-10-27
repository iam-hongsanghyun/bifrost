"""Configuration — every tunable comes from the environment, never a literal.

Bifrost owns no modelling data of its own. It talks to a running Ragnarok over
HTTP and stores one project package per conversation, so the only things it needs
configured are *where Ragnarok is* and *where its own conversation store lives*.

Mirror every variable added here into ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved settings for one process."""

    #: Base URL of the Ragnarok backend this instance drives. Ragnarok is the
    #: engine and the single source of truth for the model — Bifrost never holds
    #: a second copy.
    ragnarok_api_base: str

    #: Ragnarok session the agent and the embedded Ragnarok UI both act on. They
    #: MUST match, or the user watches a different model than the agent edits.
    ragnarok_session_id: str

    #: Where conversation groups + their project packages live.
    data_dir: Path

    #: Seconds to wait on a Ragnarok call. Solves are submitted to Ragnarok's own
    #: queue and polled, so no request here should be long-running.
    request_timeout_s: int

    #: The repo root. Exposed so the agent can put it on a subprocess PYTHONPATH
    #: without deriving it from its own nesting.
    repo_root: Path = Path(".")

    #: Most x points a chart the agent draws may carry. A year of hourly snapshots
    #: is 8,760; the old cap of 2,000 silently dropped the last ~70% of it. The
    #: browser thins what it DRAWS (and restores detail on zoom), so this guards
    #: the event stream, not the picture.
    chart_max_points: int = 10_000

    #: Most numbers (points x series) in one chart event. A 20-series year is
    #: 175,200; beyond this the event is thinned, keeping peaks, rather than refused.
    #: Matches the most the browser draws (MAX_DRAW_CELLS in lib/chartModel.ts).
    chart_max_cells: int = 250_000

    #: Most rows the State panel's table will pull from Ragnarok to sort / filter /
    #: page server-side.
    table_max_rows: int = 20_000

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"

    @property
    def agent_dir(self) -> Path:
        return self.data_dir / "agent"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / "secrets.json"

    @classmethod
    def from_env(cls) -> "Config":
        # `bifrost/config.py` → parents[1] is the repo root. Every other module
        # takes its paths from CONFIG rather than recomputing this; five modules
        # each deriving the root from their own depth is how the package rename
        # silently pointed the data dir one level too high.
        repo_root = Path(__file__).resolve().parents[1]
        return cls(
            # 8772, NOT Ragnarok's conventional 8000. The user runs their own
            # Ragnarok by hand — often a different clone for a different study —
            # and Bifrost defaulting onto that port meant it silently drove
            # whatever was there: same API, other code, other data directory, and
            # the model the agent built nowhere to be found. Its own port lets both
            # run at once; `served_root` in engine.py refuses a stranger even here.
            ragnarok_api_base=os.environ.get(
                "RAGNAROK_API_BASE", "http://127.0.0.1:8772"
            ).rstrip("/"),
            ragnarok_session_id=os.environ.get("RAGNAROK_SESSION_ID", "default"),
            data_dir=_env_path("BIFROST_DATA_DIR", repo_root / "backend" / "data"),
            repo_root=repo_root,
            request_timeout_s=_env_int("BIFROST_REQUEST_TIMEOUT_S", 120),
            chart_max_points=_env_int("BIFROST_CHART_MAX_POINTS", 10_000),
            chart_max_cells=_env_int("BIFROST_CHART_MAX_CELLS", 250_000),
            table_max_rows=_env_int("BIFROST_TABLE_MAX_ROWS", 20_000),
        )


CONFIG = Config.from_env()
