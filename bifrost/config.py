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

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"

    @classmethod
    def from_env(cls) -> "Config":
        repo_root = Path(__file__).resolve().parents[2]
        return cls(
            ragnarok_api_base=os.environ.get(
                "RAGNAROK_API_BASE", "http://127.0.0.1:8000"
            ).rstrip("/"),
            ragnarok_session_id=os.environ.get("RAGNAROK_SESSION_ID", "default"),
            data_dir=_env_path("BIFROST_DATA_DIR", repo_root / "backend" / "data"),
            request_timeout_s=_env_int("BIFROST_REQUEST_TIMEOUT_S", 120),
        )


CONFIG = Config.from_env()
