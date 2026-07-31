"""The engine: where Ragnarok lives, and whether it is running.

Bifrost is a wrapper, so it owns the question "which Ragnarok?" rather than
assuming one. The user points it at a checkout; Bifrost validates it, remembers it,
and can start and stop it. That is what makes Bifrost double-clickable: one
launcher, and the engine comes up from a directory the user chose once.

Two things this module refuses to do:

* **Guess.** A directory is only accepted when it actually looks like Ragnarok
  (``backend/app/main.py`` plus its requirements). Accepting anything and failing
  later at the first API call would report the error in the wrong place, long after
  the user could connect it to what they picked.
* **Adopt silently.** If something is already serving on the port, Bifrost uses it
  and says so rather than starting a second one. Two engines on one session would
  interleave writes to the same model.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG

logger = logging.getLogger("bifrost.engine")

#: Files that must exist for a directory to be a Ragnarok checkout. Chosen because
#: they are what Bifrost actually needs: the ASGI app it serves, and the
#: requirements that build the interpreter to serve it with.
_MARKERS = ("backend/app/main.py", "backend/requirements.txt")

#: Interpreters to try inside a checkout, in order. Ragnarok's own launcher builds
#: ``.venv-pypsa``, so that is the one that already has PyPSA and the solver.
_VENV_CANDIDATES = (".venv-pypsa/bin/python", ".venv/bin/python")


@dataclass(frozen=True)
class EngineRootCheck:
    """Why a directory was accepted or refused, in the user's terms."""

    path: str
    valid: bool
    reason: str
    interpreter: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "valid": self.valid,
            "reason": self.reason,
            "interpreter": self.interpreter,
        }


def _settings_path() -> Path:
    return CONFIG.data_dir / "engine.json"


def load_root() -> Path | None:
    """The remembered Ragnarok root, if one was chosen and still exists.

    A root that has been moved or deleted returns ``None`` rather than a dead path,
    so the UI shows the picker again instead of a confusing connection error.
    """
    path = _settings_path()
    if not path.exists():
        env = os.environ.get("RAGNAROK_ROOT", "").strip()
        return Path(env).expanduser() if env else None
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Unreadable engine settings at %s", path)
        return None
    stored = str(raw.get("root") or "").strip()
    if not stored:
        return None
    root = Path(stored).expanduser()
    return root if root.exists() else None


def save_root(root: Path) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps({"root": str(root)}, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("Engine root set to %s", root)


def find_interpreter(root: Path) -> str | None:
    """A Python inside the checkout that can serve Ragnarok.

    Only an in-checkout venv counts. The system interpreter is not offered: PyPSA,
    linopy and HiGHS are not there, so starting with it would fail at import in a
    subprocess whose output the user is not looking at.
    """
    for candidate in _VENV_CANDIDATES:
        interpreter = root / candidate
        if interpreter.exists():
            return str(interpreter)
    return None


def check_root(raw: str) -> EngineRootCheck:
    """Validate a candidate directory, with a reason either way."""
    text = (raw or "").strip()
    if not text:
        return EngineRootCheck(text, False, "No directory given.")
    root = Path(text).expanduser()
    if not root.exists():
        return EngineRootCheck(str(root), False, "That directory does not exist.")
    if not root.is_dir():
        return EngineRootCheck(str(root), False, "That is a file, not a directory.")
    missing = [m for m in _MARKERS if not (root / m).exists()]
    if missing:
        return EngineRootCheck(
            str(root), False,
            f"Not a Ragnarok checkout — missing {', '.join(missing)}.",
        )
    interpreter = find_interpreter(root)
    if interpreter is None:
        return EngineRootCheck(
            str(root), True,
            "Ragnarok found, but it has no environment yet. Run its own "
            "run.command once to build .venv-pypsa, then Bifrost can start it.",
            None,
        )
    return EngineRootCheck(str(root), True, "Ragnarok found.", interpreter)


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something already answers on the port.

    Used to adopt a Ragnarok the user started themselves instead of racing it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


class EngineProcess:
    """A Ragnarok Bifrost started. At most one per Bifrost process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._root: Path | None = None
        self._log: Path | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        """Who owns the engine on the port: Bifrost, someone else, or nobody."""
        port = _port_of(CONFIG.ragnarok_api_base)
        busy = port_busy(port)
        if self.running:
            owner = "bifrost"
        elif busy:
            owner = "external"
        else:
            owner = None
        return {
            "owner": owner,
            "running": busy,
            "port": port,
            "root": str(self._root) if self._root else None,
            "logPath": str(self._log) if self._log else None,
            "exitCode": None if self.running or self._proc is None else self._proc.poll(),
        }

    def start(self, root: Path) -> dict[str, Any]:
        """Serve Ragnarok from ``root``. Idempotent, and never a second copy."""
        port = _port_of(CONFIG.ragnarok_api_base)
        if self.running:
            return {"started": False, "reason": "Bifrost already started the engine.", **self.status()}
        if port_busy(port):
            return {
                "started": False,
                "reason": (
                    f"Something is already serving on port {port}. Bifrost is using it "
                    "rather than starting a second engine — two engines writing one "
                    "session would interleave changes to the same model."
                ),
                **self.status(),
            }
        interpreter = find_interpreter(root)
        if interpreter is None:
            raise RuntimeError(
                "That Ragnarok has no environment yet. Run its run.command once to "
                "build .venv-pypsa, then start it from here."
            )
        log_dir = CONFIG.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = log_dir / "ragnarok.log"
        # Output goes to a file, not a pipe: nobody drains a pipe here, and a full
        # pipe buffer would deadlock the engine mid-solve.
        handle = self._log.open("ab", buffering=0)
        self._proc = subprocess.Popen(
            [interpreter, "-m", "uvicorn", "backend.app.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            # Its own process group, so stopping the engine cannot signal Bifrost.
            start_new_session=True,
        )
        self._root = root
        logger.info("Started Ragnarok from %s (pid %s), log %s", root, self._proc.pid, self._log)
        return {"started": True, "reason": f"Starting Ragnarok from {root}.", **self.status()}

    def stop(self) -> dict[str, Any]:
        """Stop only an engine Bifrost started.

        One the user started themselves is left alone — Bifrost did not create it
        and closing it could discard work happening outside this app.
        """
        if not self.running:
            return {"stopped": False, "reason": "Bifrost is not running an engine.", **self.status()}
        assert self._proc is not None
        pid = self._proc.pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            self._proc.wait(timeout=5)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
        logger.info("Stopped Ragnarok (pid %s)", pid)
        return {"stopped": True, "reason": "Engine stopped.", **self.status()}

    def tail_log(self, lines: int = 60) -> list[str]:
        """The engine's own output, so a failed start is diagnosable in the UI
        rather than only in a terminal the user never opened."""
        if self._log is None or not self._log.exists():
            return []
        try:
            content = self._log.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return content[-max(1, lines):]


def _port_of(base: str) -> int:
    """Port from a base URL, defaulting to 8000 the way Ragnarok does."""
    tail = base.rsplit(":", 1)[-1]
    try:
        return int(tail.split("/")[0])
    except ValueError:
        return 8000


ENGINE = EngineProcess()


def python_hint() -> str:
    """The interpreter Bifrost itself is running on — shown in setup so a version
    mismatch is visible rather than inferred from a traceback."""
    return f"{sys.executable} ({sys.version.split()[0]})"
