"""Credential resolution for the embedded agent.

Order (first hit wins), values NEVER logged and never sent to the browser:

1. Server secrets store (Settings → API keys, or ``RAGNAROK_SECRET_*`` env):
   ``claude_code_oauth_token`` (a Claude subscription token from
   ``claude setup-token``) then ``anthropic_api_key``.
2. Process env: ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``.
3. An existing Claude Code CLI login on this machine (``~/.claude.json`` /
   ``~/.claude``): the SDK subprocess runs as the same OS user, so the CLI's
   own stored credentials just work — zero-config on a dev/desktop install.
   Server deployments use the explicit token instead.

The resolved credential is injected into the SDK subprocess environment only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .. import secrets_store

OAUTH_SECRET_NAME = "claude_code_oauth_token"
API_KEY_SECRET_NAME = "anthropic_api_key"


@dataclass(frozen=True)
class AgentAuth:
    kind: str | None  # 'oauth' | 'api_key' | 'cli' | None
    env: dict[str, str]  # vars to inject into the SDK subprocess


def _cli_login_present() -> bool:
    home = Path.home()
    return (home / ".claude.json").exists() or (home / ".claude").is_dir()


def resolve() -> AgentAuth:
    secrets = secrets_store.server_secrets()
    oauth = secrets.get(OAUTH_SECRET_NAME, "").strip() or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if oauth:
        return AgentAuth(kind="oauth", env={"CLAUDE_CODE_OAUTH_TOKEN": oauth})
    api_key = secrets.get(API_KEY_SECRET_NAME, "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if api_key:
        return AgentAuth(kind="api_key", env={"ANTHROPIC_API_KEY": api_key})
    if _cli_login_present():
        return AgentAuth(kind="cli", env={})
    return AgentAuth(kind=None, env={})
