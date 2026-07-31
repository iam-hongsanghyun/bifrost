"""Conversation attachments — files the user drops into the Bifrost chat.

Uploads land under ``backend/data/agent/uploads/<session>/`` with a random
prefix (no name collisions, no path tricks); the agent reads them through the
``read_attachment`` tool, which is hard-limited to this directory. Text files
are previewed in line windows; binary files report type + size only, so a
dropped workbook or netCDF never floods the model's context.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from ..config import CONFIG

_DATA_DIR = CONFIG.data_dir

MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_FILES_PER_UPLOAD = 20
_PREVIEW_CHAR_CAP = 8_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def root(session_id: str) -> Path:
    base = Path(os.environ.get("RAGNAROK_AGENT_UPLOAD_DIR", str(_DATA_DIR / "agent" / "uploads")))
    return base / session_id


def save(session_id: str, filename: str, stream: BinaryIO) -> dict[str, Any]:
    """Store one uploaded file; returns ``{name, storedAs, path, size}``.

    Raises ``ValueError`` when the stream exceeds :data:`MAX_FILE_BYTES`.
    """
    directory = root(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    safe = _SAFE_NAME.sub("_", filename or "file").strip("._") or "file"
    stored = f"{uuid.uuid4().hex[:8]}-{safe}"
    dest = directory / stored
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise ValueError(
                    f"{filename}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            out.write(chunk)
    return {"name": filename, "storedAs": stored, "path": str(dest), "size": size}


def _resolve_inside(session_id: str, path: str) -> Path | None:
    """Resolve ``path`` and require it inside the session's upload root."""
    base = root(session_id).resolve()
    try:
        target = Path(path).resolve()
    except OSError:
        return None
    if base == target or base in target.parents:
        return target
    # Convenience: a bare stored name (or original-ish name) instead of a path.
    candidate = (base / Path(path).name).resolve()
    if candidate.exists() and base in candidate.parents:
        return candidate
    return None


def read_preview(
    session_id: str, path: str, *, offset: int = 0, limit: int = 200
) -> dict[str, Any]:
    """A bounded text window of an attachment (or a binary/size report).

    ``offset``/``limit`` are LINES for text files. The result is shaped for the
    agent: content plus enough metadata to decide the next window.
    """
    target = _resolve_inside(session_id, path)
    if target is None or not target.is_file():
        return {"error": f"no attachment at {path!r} (attachments live only in this conversation's upload folder)"}
    size = target.stat().st_size
    with target.open("rb") as fh:
        head = fh.read(4096)
    if b"\x00" in head:
        return {
            "path": str(target), "size": size, "binary": True,
            "note": "Binary file — not text-previewable. For Excel workbooks ask "
                    "the user to open it via Workbench → Open workbook (it loads "
                    "into the session); CSV/JSON/text preview fine.",
        }
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 2_000))
    lines: list[str] = []
    total = 0
    chars = 0
    truncated_line = False
    with target.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            total = i + 1
            if i < offset or len(lines) >= limit or chars >= _PREVIEW_CHAR_CAP:
                continue
            snippet = line.rstrip("\n")
            if len(snippet) > 500:
                snippet = snippet[:500] + "…"
                truncated_line = True
            lines.append(snippet)
            chars += len(snippet)
    return {
        "path": str(target), "size": size, "binary": False,
        "totalLines": total, "offset": offset, "returned": len(lines),
        "truncatedLongLines": truncated_line,
        "content": "\n".join(lines),
    }


def list_files(session_id: str) -> list[dict[str, Any]]:
    directory = root(session_id)
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.iterdir()):
        if p.is_file():
            out.append({"storedAs": p.name, "path": str(p), "size": p.stat().st_size})
    return out
