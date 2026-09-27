# Orbital — An operating system for AI agents
# Copyright (C) 2026 Orbital Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Live Claude Code model-list fetcher.

The claude-code settings dropdown used a hardcoded whitelist that went stale
every model generation. The CLI already publishes the account's list: its
stream-json ``initialize`` control response carries ``models[]``
(``value`` / ``resolvedModel`` / ``displayName`` ...), and answering it makes
no API call. ``value`` is exactly what ``--model`` accepts (aliases like
``opus``, pins like ``claude-opus-5``, variants like ``claude-fable-5-1[1m]``).

Same shape as ``codex_models``: a pure protocol layer, a spawn layer that
NEVER raises (any failure returns None and the settings page falls back to
the static whitelist), and a TTL cache so a settings load doesn't spawn the
CLI every time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time

from agent_os.agent.transports.jsonl_stream import read_jsonl_line
from agent_os.utils.subprocess_flags import win_no_window_flags

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0
_SUCCESS_TTL = 600.0
_FAILURE_TTL = 60.0
_REQUEST_ID = "orbital-models"

# The CLI's own "Default (recommended)" entry. Orbital already expresses
# "use the CLI default" as an empty setting, so offering it would be a
# second spelling of the same choice.
_SKIP_VALUES = {"default"}

_cache: dict[str, tuple[float, list[dict] | None]] = {}
_cache_lock = asyncio.Lock()


async def read_model_values(reader, writer, *, timeout: float = _DEFAULT_TIMEOUT
                            ) -> list[str]:
    """The ``models[].value`` list only (see :func:`read_models`)."""
    return [m["value"] for m in await read_models(reader, writer, timeout=timeout)]


async def read_models(reader, writer, *, timeout: float = _DEFAULT_TIMEOUT
                      ) -> list[dict]:
    """Send ``initialize`` and return ``[{"value", "label"}]`` in the CLI's
    order, deduplicated by value. ``label`` is the CLI's own ``displayName``
    ("Opus 5.5"), falling back to the value. Raises on EOF, timeout or an
    error response."""
    writer.write((json.dumps({
        "type": "control_request",
        "request_id": _REQUEST_ID,
        "request": {"subtype": "initialize"},
    }) + "\n").encode("utf-8"))
    await writer.drain()
    while True:
        line = await asyncio.wait_for(read_jsonl_line(reader), timeout)
        if not line:
            raise RuntimeError("claude closed the stream before initializing")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or msg.get("type") != "control_response":
            continue
        response = msg.get("response") or {}
        if response.get("subtype") != "success":
            raise RuntimeError(f"claude initialize failed: {response}")
        models = (response.get("response") or {}).get("models")
        if not isinstance(models, list):
            raise RuntimeError("claude initialize returned no models list")
        out: list[dict] = []
        seen: set[str] = set()
        for entry in models:
            value = entry.get("value") if isinstance(entry, dict) else None
            if (not isinstance(value, str) or not value
                    or value in _SKIP_VALUES or value in seen):
                continue
            label = entry.get("displayName")
            seen.add(value)
            out.append({"value": value,
                        "label": label if isinstance(label, str) and label else value})
        return out


async def fetch_claude_models(binary: str = "claude", *,
                              timeout: float = _DEFAULT_TIMEOUT
                              ) -> list[dict] | None:
    """Spawn the CLI in stream-json mode just long enough to answer
    ``initialize``. Returns ``[{"value", "label"}]``, or None on ANY failure."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--output-format", "stream-json", "--verbose",
            "--input-format", "stream-json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # A neutral cwd: no project context is needed, and the probe must
            # not touch a real workspace.
            cwd=tempfile.gettempdir(),
            limit=1024 * 1024,
            creationflags=win_no_window_flags(),
        )
        return await read_models(proc.stdout, proc.stdin, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — display data, never raise
        logger.info("claude model list unavailable (%s: %s) — settings fall "
                    "back to the static whitelist", type(exc).__name__, exc)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except Exception:  # noqa: BLE001
                pass


async def get_claude_models_cached(binary: str = "claude", *,
                                   ttl: float = _SUCCESS_TTL,
                                   failure_ttl: float = _FAILURE_TTL,
                                   timeout: float = _DEFAULT_TIMEOUT
                                   ) -> list[dict] | None:
    """TTL-cached :func:`fetch_claude_models`, keyed by binary path; the lock
    stops concurrent settings loads from spawning parallel CLIs."""
    async with _cache_lock:
        entry = _cache.get(binary)
        if entry is not None and time.monotonic() < entry[0]:
            return entry[1]
        values = await fetch_claude_models(binary, timeout=timeout)
        expiry = time.monotonic() + (ttl if values is not None else failure_ttl)
        _cache[binary] = (expiry, values)
        return values


def clear_claude_models_cache() -> None:
    """Drop every cached entry (settings refresh, tests)."""
    _cache.clear()
