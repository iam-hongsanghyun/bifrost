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
        )


CONFIG = Config.from_env()
