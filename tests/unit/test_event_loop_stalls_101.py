# Orbital — An operating system for AI agents
# Copyright (C) 2026 Orbital Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Spec 101 — turn start must not block the daemon's event loop.

The daemon is one asyncio loop serving every project. Sending a message ran a
cold ``SetupEngine.check_all()`` (a subprocess probe of every agent CLI, 2-10 s)
synchronously at turn start, and every request of every project queued behind
it: "after sending a message, creating or switching a session anywhere freezes
for 2-3 s". These tests pin the fix:

  - ``check_all`` serves an expired result (``allow_stale``) and refreshes it
    in ONE background sweep; concurrent cold callers share one sweep;
    ``invalidate_cache`` drops the stale copy too.
  - the config build, the hydrate-on-inject load and the pinned recap's
    session read run in worker threads, so a slow one leaves the loop free.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_os.agents import setup_engine as setup_engine_mod
from agent_os.agents.setup_engine import SetupEngine
from agent_os.daemon_v2.agent_manager import AgentManager
from agent_os.daemon_v2.models import make_session_key
from tests.card_doubles import FakeCardStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _status(slug, installed=True):
    return SimpleNamespace(slug=slug, installed=installed, name=slug)


def _engine(tmp_path, slugs=("claude-code", "codex"), delay=0.0):
    """A real SetupEngine whose per-agent probe is a counted stub."""
    registry = MagicMock()
    registry.list_all.return_value = [SimpleNamespace(slug=s) for s in slugs]
    engine = SetupEngine(registry, data_dir=str(tmp_path))
    probes = []
    lock = threading.Lock()

    def check_agent(slug):
        with lock:
            probes.append(slug)
        time.sleep(delay)
        return _status(slug)

    engine.check_agent = check_agent
    return engine, probes


def _sweeps(probes, slugs=("claude-code", "codex")):
    return len(probes) / len(slugs)


def _expire(engine):
    results, _ = engine._check_all_cache
    engine._check_all_cache = (results, time.monotonic() - 1)


async def _max_loop_gap(coro, tick=0.02):
    """Run ``coro`` while a ticker measures the worst event-loop delay."""
    gaps = []
    done = False

    async def ticker():
        last = time.monotonic()
        while not done:
            await asyncio.sleep(tick)
            now = time.monotonic()
            gaps.append(now - last - tick)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(tick * 2)  # the ticker is waiting before coro starts
    try:
        result = await coro
    finally:
        done = True
        await task
    return result, max(gaps)


# ---------------------------------------------------------------------------
# SetupEngine.check_all: stale-serve, single-flight, invalidation
# ---------------------------------------------------------------------------

class TestCheckAllStaleServe:
    def test_fresh_cache_is_served_without_a_probe(self, tmp_path):
        engine, probes = _engine(tmp_path)
        first = engine.check_all()
        assert engine.check_all() is first
        assert engine.check_all(allow_stale=True) is first
        assert _sweeps(probes) == 1

    def test_default_call_still_reprobes_an_expired_cache(self, tmp_path):
        engine, probes = _engine(tmp_path)
        engine.check_all()
        _expire(engine)
        engine.check_all()
        assert _sweeps(probes) == 2

    def test_allow_stale_returns_expired_copy_at_once_and_refreshes_once(self, tmp_path):
        engine, probes = _engine(tmp_path, delay=0.3)
        first = engine.check_all()
        _expire(engine)

        start = time.monotonic()
        served = [engine.check_all(allow_stale=True) for _ in range(5)]
        elapsed = time.monotonic() - start

        assert all(r is first for r in served)
        assert elapsed < 0.2, f"stale serve waited for the probe ({elapsed:.2f}s)"
        engine._refresh_thread.join(timeout=5)
        assert _sweeps(probes) == 2  # the initial one + ONE background refresh
        fresh = engine.check_all()
        assert fresh is not first
        assert _sweeps(probes) == 2  # the refresh filled the cache

    def test_allow_stale_on_an_empty_cache_probes(self, tmp_path):
        engine, probes = _engine(tmp_path)
        result = engine.check_all(allow_stale=True)
        assert [s.slug for s in result] == ["claude-code", "codex"]
        assert _sweeps(probes) == 1

    def test_concurrent_cold_calls_share_one_sweep(self, tmp_path):
        engine, probes = _engine(tmp_path, delay=0.2)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(engine.check_all()))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(results) == 6
        assert _sweeps(probes) == 1
        assert all(r is results[0] for r in results)

    def test_invalidate_drops_the_stale_copy(self, tmp_path):
        engine, probes = _engine(tmp_path)
        first = engine.check_all()
        _expire(engine)
        engine.invalidate_cache()
        result = engine.check_all(allow_stale=True)
        assert result is not first  # re-probed, not the pre-invalidation copy
        assert _sweeps(probes) == 2

    def test_invalidate_during_a_sweep_keeps_its_result_out_of_the_cache(self, tmp_path):
        engine, probes = _engine(tmp_path, delay=0.3)
        out = []
        t = threading.Thread(target=lambda: out.append(engine.check_all()))
        t.start()
        time.sleep(0.1)          # the sweep is in flight
        engine.invalidate_cache()  # e.g. an install finished meanwhile
        t.join(timeout=5)
        assert len(out) == 1      # its caller still gets an answer
        assert engine._check_all_cache is None
        engine.check_all()
        assert _sweeps(probes) == 2

    def test_ttl_constant_unchanged(self):
        assert setup_engine_mod.CHECK_ALL_CACHE_TTL_SECONDS == 60


# ---------------------------------------------------------------------------
# AgentManager: config build + hydrate off the loop
# ---------------------------------------------------------------------------

PID = "proj_a"
SID = "proj_a_sess1"


class _SlowEngine:
    """Stands in for a cold probe sweep: blocks the calling thread."""

    def __init__(self, delay):
        self.delay = delay
        self.threads = []

    def check_all(self, allow_stale=False):
        self.threads.append(threading.current_thread())
        time.sleep(self.delay)
        return [_status("claude-code"), _status("codex"), _status("gemini-cli"),
                _status("built-in"), _status("pi", installed=False)]


def _manager(tmp_path, setup_engine=None):
    project = {"project_id": PID, "name": "A", "workspace": str(tmp_path),
               "disabled_sub_agents": ["gemini-cli"]}
    project_store = MagicMock()
    project_store.get_project = MagicMock(
        side_effect=lambda pid: project if pid == PID else None)
    project_store.list_projects = MagicMock(return_value=[project])
    credential_store = MagicMock()
    credential_store.get_api_key = MagicMock(return_value=None)
    return AgentManager(
        project_store=project_store,
        ws_manager=MagicMock(),
        sub_agent_manager=MagicMock(),
        activity_translator=MagicMock(),
        process_manager=MagicMock(),
        settings_store=FakeCardStore.with_default(),
        credential_store=credential_store,
        setup_engine=setup_engine,
    )


@pytest.mark.asyncio
async def test_config_build_with_a_slow_probe_leaves_the_loop_free(tmp_path):
    engine = _SlowEngine(delay=1.0)
    mgr = _manager(tmp_path, setup_engine=engine)

    cfg, gap = await _max_loop_gap(mgr._abuild_agent_config_from_project(PID))

    assert gap < 0.3, f"event loop stalled {gap:.2f}s behind check_all"
    assert engine.threads and engine.threads[0] is not threading.main_thread()
    # Same enabled list as before: installed, minus built-in and the denylist.
    assert cfg.enabled_sub_agents == ["claude-code", "codex"]
    assert cfg.disabled_sub_agents == ["gemini-cli"]


def _idle_handle():
    handle = MagicMock()
    handle.task = None
    handle.loop.run = AsyncMock()
    return handle


def _stub_resume(mgr):
    mgr._provider_config_changed = MagicMock(return_value=False)
    mgr._touch_card = MagicMock()
    mgr._broadcast = MagicMock()
    mgr._on_loop_done = MagicMock(return_value=lambda task: None)


@pytest.mark.asyncio
async def test_hot_resume_with_a_slow_probe_leaves_the_loop_free(tmp_path):
    """Every message to an idle live session goes through _start_loop."""
    engine = _SlowEngine(delay=1.0)
    mgr = _manager(tmp_path, setup_engine=engine)
    _stub_resume(mgr)
    handle = _idle_handle()
    mgr._handles[make_session_key(PID, SID)] = handle

    _, gap = await _max_loop_gap(mgr._start_loop(PID, session_id=SID))
    await handle.task

    assert gap < 0.3, f"event loop stalled {gap:.2f}s at turn start"
    handle.loop.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_resumes_start_one_run(tmp_path):
    """The off-loop build opens a window; a second resume must not start a
    second loop.run() on the same session."""
    mgr = _manager(tmp_path, setup_engine=_SlowEngine(delay=0.2))
    _stub_resume(mgr)
    handle = _idle_handle()
    mgr._handles[make_session_key(PID, SID)] = handle

    await asyncio.gather(
        mgr._start_loop(PID, session_id=SID),
        mgr._start_loop(PID, session_id=SID),
    )
    await handle.task
    handle.loop.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_hydrate_on_inject_loads_off_the_loop(tmp_path):
    mgr = _manager(tmp_path, setup_engine=_SlowEngine(delay=0.0))
    loaded = SimpleNamespace(session_uuid=SID, session_id=SID)
    load_threads = []

    def slow_load(project_id, identifier):
        load_threads.append(threading.current_thread())
        time.sleep(1.0)  # a long, image-heavy JSONL
        return loaded

    mgr._load_session_from_disk = slow_load
    mgr._start_with_persisted_message = AsyncMock(return_value="started")

    result, gap = await _max_loop_gap(
        mgr.inject_message(PID, "hello", session_id=SID))

    assert result == "started"
    assert gap < 0.3, f"event loop stalled {gap:.2f}s on hydrate"
    assert load_threads[0] is not threading.main_thread()
    kwargs = mgr._start_with_persisted_message.await_args.kwargs
    assert kwargs["session"] is loaded
    assert kwargs["session_id"] == SID


@pytest.mark.asyncio
async def test_inject_that_loses_the_hydrate_race_takes_the_live_handle(tmp_path):
    """While the hydrate ran off-loop another request started the session:
    the message must be queued on that live run, not rejected."""
    mgr = _manager(tmp_path, setup_engine=_SlowEngine(delay=0.0))
    live = MagicMock()
    live.task = MagicMock()
    live.task.done.return_value = False  # a run is in progress
    live.session._paused_for_approval = False
    key = make_session_key(PID, SID)

    def load_then_lose_race(project_id, identifier):
        mgr._handles[key] = live  # the other request registered meanwhile
        return SimpleNamespace(session_uuid=SID, session_id=SID)

    mgr._load_session_from_disk = load_then_lose_race
    mgr._start_with_persisted_message = AsyncMock(return_value="started")

    result = await mgr.inject_message(PID, "hello", session_id=SID)

    assert result == "queued_same_session"
    live.session.queue_message.assert_called_once_with("hello", nonce=None)
    mgr._start_with_persisted_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# SubAgentManager recap: the session read runs off the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recap_session_read_leaves_the_loop_free():
    from agent_os.daemon_v2.recap import build_recap
    from agent_os.daemon_v2.sub_agent_manager import SubAgentManager

    messages = [
        {"role": "user", "content": "first question", "source": "user"},
        {"role": "assistant", "content": "first answer", "source": "management"},
    ]
    resolver_threads = []

    def slow_resolver(project_id, session_id):
        resolver_threads.append(threading.current_thread())
        time.sleep(1.0)  # evicted handle -> full JSONL parse from disk
        return SimpleNamespace(get_messages=lambda: list(messages))

    sam = SubAgentManager(process_manager=MagicMock())
    sam._session_resolver = slow_resolver
    adapter = SimpleNamespace(_resume_status=("fresh", "first_spawn"))

    block, gap = await _max_loop_gap(
        sam._build_recap(adapter, PID, SID, "claude-code"))

    assert gap < 0.3, f"event loop stalled {gap:.2f}s on the recap read"
    assert resolver_threads[0] is not threading.main_thread()
    assert block == build_recap(messages, "claude-code", fresh=True)
    assert adapter._recap_primed is True


# ---------------------------------------------------------------------------
# Pinned direct-send path: worker spawn + the persisted user row
# ---------------------------------------------------------------------------

def _spawn_manager(tmp_path, *, probe_delay=0.0, resolver=None):
    """A SubAgentManager whose spawn stops right after the resume decision
    (transport construction raises), with a REAL SetupEngine whose check_all
    cache has expired and whose probe sweep is slow."""
    from agent_os.daemon_v2.sub_agent_manager import SubAgentManager

    engine, probes = _engine(tmp_path, delay=probe_delay)
    engine.check_all()
    _expire(engine)
    engine.get_adapter_config = MagicMock(return_value={
        "command": "codex", "workspace": str(tmp_path), "env": {}})
    manifest = SimpleNamespace(
        slug="codex",
        runtime=SimpleNamespace(adapter="cli", transport="pty", mode=None))
    registry = MagicMock()
    registry.get.return_value = manifest
    project_store = MagicMock()
    project_store.get_project.return_value = {
        "project_id": PID, "workspace": str(tmp_path)}
    sam = SubAgentManager(process_manager=MagicMock(), registry=registry,
                          setup_engine=engine, project_store=project_store)
    sam._session_resolver = resolver
    sam._resolve_transport = MagicMock(side_effect=ValueError("stop here"))
    return sam, engine, probes


@pytest.mark.asyncio
async def test_worker_spawn_with_an_expired_probe_cache_leaves_the_loop_free(tmp_path):
    resolver_threads = []

    def slow_resolver(project_id, session_id):
        resolver_threads.append(threading.current_thread())
        time.sleep(1.0)  # evicted pinned chat -> full JSONL parse
        return SimpleNamespace(get_sub_agent_thread=lambda handle: None)

    sam, engine, probes = _spawn_manager(
        tmp_path, probe_delay=0.5, resolver=slow_resolver)

    result, gap = await _max_loop_gap(
        sam._start_from_registry(PID, "codex", session_id=SID, pinned=True))

    assert result.startswith("Error: unsupported transport")  # reached the end
    assert gap < 0.3, f"event loop stalled {gap:.2f}s on worker spawn"
    assert resolver_threads and resolver_threads[0] is not threading.main_thread()
    # The stale list was served; the refresh ran in the background.
    engine._refresh_thread.join(timeout=5)
    assert _sweeps(probes) == 2


@pytest.mark.asyncio
async def test_background_loss_note_reads_the_session_off_the_loop():
    from agent_os.daemon_v2.sub_agent_manager import SubAgentManager

    writes = []
    session = SimpleNamespace(
        get_sub_agent_thread=lambda handle: {"session_id": "cc-1", "model": "m"},
        set_sub_agent_thread=lambda handle, **kw: writes.append(
            (threading.current_thread(), kw)),
    )

    def slow_resolver(project_id, session_id):
        time.sleep(1.0)
        return session

    sam = SubAgentManager(process_manager=MagicMock())
    sam._session_resolver = slow_resolver

    _, gap = await _max_loop_gap(
        sam._report_background_loss(PID, SID, "claude-code", ["sleep 100"]))

    assert gap < 0.3, f"event loop stalled {gap:.2f}s on the loss note"
    assert writes and writes[0][0] is threading.main_thread()  # write on the loop
    assert writes[0][1]["background_loss"] is True


@pytest.mark.asyncio
async def test_persist_mention_message_loads_off_the_loop_and_appends_on_it(tmp_path):
    mgr = _manager(tmp_path)
    appends = []
    loaded = SimpleNamespace(
        append=lambda msg: appends.append((threading.current_thread(), msg)))
    load_threads = []

    def slow_load(project_id, identifier):
        load_threads.append(threading.current_thread())
        time.sleep(1.0)
        return loaded

    mgr._load_session_from_disk = slow_load
    user_msg = {"role": "user", "content": "hi", "target": "codex"}

    resolved, gap = await _max_loop_gap(
        mgr.persist_mention_message(PID, SID, user_msg))

    assert resolved == SID
    assert gap < 0.3, f"event loop stalled {gap:.2f}s persisting the pinned row"
    assert load_threads[0] is not threading.main_thread()
    # The append takes the session file lock: it stays on the loop.
    assert appends == [(threading.main_thread(), user_msg)]


@pytest.mark.asyncio
async def test_persist_mention_message_prefers_a_handle_that_appeared_meanwhile(tmp_path):
    mgr = _manager(tmp_path)
    live = MagicMock()
    stale_copy = MagicMock()
    key = make_session_key(PID, SID)

    def load_then_lose_race(project_id, identifier):
        mgr._handles[key] = live
        return stale_copy

    mgr._load_session_from_disk = load_then_lose_race
    user_msg = {"role": "user", "content": "hi", "target": "codex"}

    assert await mgr.persist_mention_message(PID, SID, user_msg) == SID
    live.session.append.assert_called_once_with(user_msg)
    stale_copy.append.assert_not_called()
