# Orbital — An operating system for AI agents
# Copyright (C) 2026 Orbital Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Subscription quota snapshots for claude-code and codex (spec 099 §3.2).

One daemon-wide store, keyed by agent slug. The quota is ACCOUNT-wide — not
per handle, not per project — so any turn in any project refreshes it, and
the latest snapshot is persisted next to the settings store so it shows
before the first turn after a restart.

Sources (both live-probed 2026-09-27, spec §2.1):

- claude-code is push-only: the CLI emits a ``rate_limit_event`` on every
  turn, and the per-window split lives only in
  ``RateLimitInfo.raw["unifiedWindows"]``. Nothing is known until a turn
  (or a persisted snapshot) — never spend a turn to learn it.
- codex answers ``account/rateLimits/read`` on demand and pushes sparse
  ``account/rateLimits/updated`` notifications. Windows are identified by
  ``windowDurationMins``; the primary/secondary order is not promised.

The quota is display data only. It never enters a transcript, a session
JSONL, the manager's context, or a chat WS event: the transports hand it
straight to this store (the same side-channel pattern as their ledger
usage capture), and the store's change hook broadcasts
``agent.quota_updated``. Neither payload is a stable public contract, so
every parse is defensive: a malformed field drops that window and nothing
here ever raises into a turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

QUOTA_AGENTS = ("claude-code", "codex")

# claude ``unifiedWindows`` keys -> snapshot window kinds. ``overage`` is a
# pay-as-you-go state, not a subscription window.
_CLAUDE_WINDOW_KINDS = {
    "five_hour": "five_hour",
    "seven_day": "weekly",
    "seven_day_opus": "weekly_opus",
    "seven_day_sonnet": "weekly_sonnet",
}

# codex ``windowDurationMins`` -> kind. Unknown durations are dropped.
_CODEX_WINDOW_KINDS = {300: "five_hour", 10080: "weekly"}

# The codex on-demand read: at most one per interval (success or not), one
# in flight, bounded.
CODEX_REFRESH_INTERVAL_S = 60.0
CODEX_READ_TIMEOUT_S = 10.0

_FILE_VERSION = 1

OnChange = Callable[[str, "dict | None"], None]
CodexReader = Callable[[], Awaitable["dict | None"]]


def _now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat()


def _ts_iso(value: Any) -> str | None:
    """Unix seconds -> ISO-8601 UTC, or None when absent/malformed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _pct(value: Any, scale: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    pct = float(value) * scale
    if pct != pct:  # NaN
        return None
    return round(min(max(pct, 0.0), 100.0), 1)


def _window(kind: str, used_pct: float | None, resets_at: Any) -> dict | None:
    if used_pct is None:
        return None
    return {"kind": kind, "used_pct": used_pct, "resets_at": _ts_iso(resets_at)}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_claude_rate_limit(info: Any, *, status: str | None = None,
                                clock: Callable[[], float] = time.time
                                ) -> dict | None:
    """claude ``rate_limit_info`` -> snapshot, or None when nothing usable.

    ``info`` is the SDK's ``RateLimitInfo`` (its ``raw`` dict is read) or the
    raw dict itself.
    """
    raw = getattr(info, "raw", info)
    if not isinstance(raw, dict):
        return None
    status = status or getattr(info, "status", None) or raw.get("status")
    windows: list[dict] = []
    unified = raw.get("unifiedWindows")
    if isinstance(unified, dict):
        for key, kind in _CLAUDE_WINDOW_KINDS.items():
            entry = unified.get(key)
            if not isinstance(entry, dict):
                continue
            w = _window(kind, _pct(entry.get("utilization"), 100.0),
                        entry.get("resetsAt"))
            if w is not None:
                windows.append(w)
    else:
        # Older CLIs: only the headline window is known.
        kind = _CLAUDE_WINDOW_KINDS.get(raw.get("rateLimitType") or "")
        if kind:
            w = _window(kind, _pct(raw.get("utilization"), 100.0),
                        raw.get("resetsAt"))
            if w is not None:
                windows.append(w)
    limited = status == "rejected"
    if not windows and not limited:
        return None
    return {
        "agent": "claude-code",
        "observed_at": _now_iso(clock),
        "plan": None,
        "limited": limited,
        "windows": windows,
    }


def _codex_windows(snapshot: dict) -> list[dict]:
    windows: list[dict] = []
    for slot in ("primary", "secondary"):
        entry = snapshot.get(slot)
        if not isinstance(entry, dict):
            continue
        kind = _CODEX_WINDOW_KINDS.get(entry.get("windowDurationMins"))
        if kind is None or any(w["kind"] == kind for w in windows):
            continue
        w = _window(kind, _pct(entry.get("usedPercent"), 1.0),
                    entry.get("resetsAt"))
        if w is not None:
            windows.append(w)
    return windows


def normalize_codex_snapshot(snapshot: Any, *,
                             clock: Callable[[], float] = time.time
                             ) -> dict | None:
    """codex ``RateLimitSnapshot`` -> snapshot, or None when nothing usable."""
    if not isinstance(snapshot, dict):
        return None
    windows = _codex_windows(snapshot)
    limited = snapshot.get("rateLimitReachedType") is not None
    if not windows and not limited:
        return None
    plan = snapshot.get("planType")
    return {
        "agent": "codex",
        "observed_at": _now_iso(clock),
        "plan": plan if isinstance(plan, str) and plan else None,
        "limited": limited,
        "windows": windows,
    }


def pick_codex_snapshot(read_result: Any) -> dict | None:
    """The codex bucket of an ``account/rateLimits/read`` result:
    ``rateLimitsByLimitId["codex"]``, falling back to ``rateLimits``."""
    if not isinstance(read_result, dict):
        return None
    by_id = read_result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        return by_id["codex"]
    snap = read_result.get("rateLimits")
    return snap if isinstance(snap, dict) else None


def _valid_snapshot(agent: str, snap: Any) -> bool:
    return (agent in QUOTA_AGENTS and isinstance(snap, dict)
            and isinstance(snap.get("windows"), list)
            and isinstance(snap.get("observed_at"), str))


def _observed_epoch(snap: dict) -> float | None:
    try:
        return datetime.fromisoformat(snap["observed_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class QuotaStore:
    """Latest quota snapshot per agent, persisted atomically off the loop."""

    def __init__(self, path: str | None = None, *,
                 on_change: OnChange | None = None,
                 clock: Callable[[], float] = time.time,
                 codex_timeout: float = CODEX_READ_TIMEOUT_S,
                 codex_interval: float = CODEX_REFRESH_INTERVAL_S) -> None:
        self._path = path
        self._on_change = on_change
        self._clock = clock
        self._codex_timeout = codex_timeout
        self._codex_interval = codex_interval
        self._snapshots: dict[str, dict] = {}
        self._lock = threading.Lock()          # guards _snapshots + the file
        self._codex_task: asyncio.Task | None = None
        self._codex_attempted_at: float | None = None
        self._pending_writes: set[asyncio.Future] = set()
        self._load()

    def set_on_change(self, on_change: OnChange | None) -> None:
        self._on_change = on_change

    # -- reads ---------------------------------------------------------

    def get(self, agent: str) -> dict | None:
        with self._lock:
            snap = self._snapshots.get(agent)
            return dict(snap) if snap is not None else None

    def snapshots(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._snapshots.items()}

    def age_seconds(self, agent: str) -> float | None:
        snap = self.get(agent)
        observed = _observed_epoch(snap) if snap else None
        return None if observed is None else max(0.0, self._clock() - observed)

    # -- writes --------------------------------------------------------

    def record(self, agent: str, snapshot: dict) -> None:
        if not _valid_snapshot(agent, snapshot):
            return
        with self._lock:
            self._snapshots[agent] = dict(snapshot)
        self._persist()
        self._notify(agent, dict(snapshot))

    def clear(self, agent: str) -> None:
        """Drop an agent's snapshot (account switch: it belongs to the old
        account)."""
        with self._lock:
            existed = self._snapshots.pop(agent, None) is not None
        if existed:
            self._persist()
            self._notify(agent, None)

    def merge_codex_push(self, pushed: Any) -> None:
        """Fold a sparse ``account/rateLimits/updated`` snapshot into the
        stored one: pushed windows replace same-kind windows, a null plan
        does not clear the known plan (schema: nullable metadata "does not
        clear a previously observed value")."""
        if not isinstance(pushed, dict):
            return
        limit_id = pushed.get("limitId")
        if limit_id not in (None, "codex"):
            return
        fresh = normalize_codex_snapshot(pushed, clock=self._clock)
        if fresh is None:
            return
        prior = self.get("codex")
        if prior is not None:
            kinds = {w["kind"] for w in fresh["windows"]}
            fresh["windows"] = fresh["windows"] + [
                w for w in prior.get("windows", []) if w.get("kind") not in kinds]
            fresh["plan"] = fresh["plan"] or prior.get("plan")
            if not fresh["limited"]:
                fresh["limited"] = any(
                    (w.get("used_pct") or 0) >= 100 for w in fresh["windows"])
        self.record("codex", fresh)

    # -- codex on-demand read -----------------------------------------

    def ensure_codex_fresh(self, reader: CodexReader) -> asyncio.Task | None:
        """Start (or join) a codex read when the snapshot is stale.

        Returns the in-flight task, or None when the snapshot is fresh or a
        read was attempted within the interval (success or not). Must be
        called on the event loop; the read itself is async and bounded.
        """
        if self._codex_task is not None and not self._codex_task.done():
            return self._codex_task
        now = self._clock()
        age = self.age_seconds("codex")
        if age is not None and age < self._codex_interval:
            return None
        if (self._codex_attempted_at is not None
                and now - self._codex_attempted_at < self._codex_interval):
            return None
        self._codex_attempted_at = now
        self._codex_task = asyncio.get_running_loop().create_task(
            self._read_codex(reader), name="quota-codex-read")
        return self._codex_task

    async def _read_codex(self, reader: CodexReader) -> dict | None:
        try:
            result = await asyncio.wait_for(reader(), self._codex_timeout)
        except asyncio.TimeoutError:
            logger.info("quota: codex rate-limit read timed out")
            return None
        except Exception as exc:  # noqa: BLE001 — display data, never raise
            logger.info("quota: codex rate-limit read failed (%s: %s)",
                        type(exc).__name__, exc)
            return None
        snap = normalize_codex_snapshot(pick_codex_snapshot(result),
                                        clock=self._clock)
        if snap is None:
            return None
        self.record("codex", snap)
        return snap

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("snapshots") if isinstance(data, dict) else None
            if not isinstance(rows, dict):
                return
            for agent, snap in rows.items():
                if _valid_snapshot(agent, snap):
                    self._snapshots[agent] = snap
        except Exception:  # noqa: BLE001 — a bad file is an empty store
            logger.warning("quota: ignoring unreadable %s", self._path,
                           exc_info=True)

    def _write_file(self) -> None:
        """Write the CURRENT state (tmp + rename). Serialized by the lock, and
        each write reads the state under it, so the last write to finish is
        always the newest state regardless of executor ordering."""
        if not self._path:
            return
        try:
            with self._lock:
                payload = {"version": _FILE_VERSION,
                           "snapshots": {k: dict(v) for k, v in
                                         self._snapshots.items()}}
                directory = os.path.dirname(os.path.abspath(self._path))
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".quota-", suffix=".tmp",
                                           dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(payload, f)
                    os.replace(tmp, self._path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
        except Exception:  # noqa: BLE001
            logger.warning("quota: could not persist %s", self._path,
                           exc_info=True)

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_file()
            return
        fut = loop.run_in_executor(None, self._write_file)
        self._pending_writes.add(fut)
        fut.add_done_callback(self._pending_writes.discard)

    async def drain_writes(self) -> None:
        """Await in-flight persistence (tests, shutdown)."""
        if self._pending_writes:
            await asyncio.gather(*list(self._pending_writes),
                                 return_exceptions=True)

    def _notify(self, agent: str, snapshot: dict | None) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(agent, snapshot)
        except Exception:  # noqa: BLE001
            logger.warning("quota: change hook failed", exc_info=True)


# ---------------------------------------------------------------------------
# Process-wide store: the transports' entry point
# ---------------------------------------------------------------------------

_store: QuotaStore | None = None


def install(store: QuotaStore | None) -> None:
    """Make ``store`` the daemon-wide store (app factory; tests reset it)."""
    global _store
    _store = store


def get_quota_store() -> QuotaStore | None:
    return _store


def publish_claude_rate_limit(info: Any) -> None:
    """Record a claude-code ``rate_limit_event``. Never raises; a no-op when
    no store is installed (unit tests, tooling)."""
    store = _store
    if store is None:
        return
    try:
        snap = normalize_claude_rate_limit(info)
        if snap is not None:
            store.record("claude-code", snap)
    except Exception:  # noqa: BLE001
        logger.warning("quota: claude rate-limit capture failed",
                       exc_info=True)


def publish_codex_snapshot(snapshot: Any, *, push: bool = False) -> None:
    """Record a codex ``RateLimitSnapshot`` — merged when it is a sparse
    push, replacing when it is a full read. Never raises."""
    store = _store
    if store is None:
        return
    try:
        if push:
            store.merge_codex_push(snapshot)
            return
        snap = normalize_codex_snapshot(snapshot)
        if snap is not None:
            store.record("codex", snap)
    except Exception:  # noqa: BLE001
        logger.warning("quota: codex rate-limit capture failed", exc_info=True)
