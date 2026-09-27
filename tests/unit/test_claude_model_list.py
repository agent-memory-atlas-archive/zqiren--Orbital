# Orbital — An operating system for AI agents
# Copyright (C) 2026 Orbital Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Claude Code live model-list fetcher.

The claude-code model dropdown was a hardcoded whitelist that went stale every
model generation (Opus 5.5 / Fable 5.1 shipped and never appeared). The CLI
already publishes the account's model list: the stream-json ``initialize``
control response carries ``models[]`` (value, resolvedModel, displayName...)
without any API call. The fetcher mirrors ``codex_models``: a pure protocol
layer over in-memory streams, a spawn layer that never raises, and a TTL cache.
"""

import asyncio
import json
import sys

import pytest

from agent_os.agent.transports import claude_models


class FakeWriter:
    def __init__(self):
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        for raw in data.decode("utf-8").splitlines():
            if raw.strip():
                self.lines.append(json.loads(raw))

    async def drain(self) -> None:
        return None


def _feed(reader: asyncio.StreamReader, *objs: dict) -> None:
    for obj in objs:
        reader.feed_data((json.dumps(obj) + "\n").encode("utf-8"))


# Shape observed live from claude 2.1.283 (values trimmed).
INIT_RESPONSE = {
    "type": "control_response",
    "response": {
        "subtype": "success",
        "request_id": "orbital-models",
        "response": {
            "models": [
                {"value": "default", "resolvedModel": "claude-opus-5-5",
                 "displayName": "Default (recommended)"},
                {"value": "opus", "resolvedModel": "claude-opus-5-5",
                 "displayName": "Opus 5.5"},
                {"value": "claude-fable-5-1[1m]", "resolvedModel": "claude-fable-5-1",
                 "displayName": "Fable 5.1"},
                {"value": "sonnet", "resolvedModel": "claude-sonnet-5",
                 "displayName": "Sonnet 5"},
                {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001",
                 "displayName": "Haiku 4.5"},
                {"value": "claude-opus-5", "displayName": "Opus 5"},
                {"value": "opus"},          # duplicate: kept once
                {"displayName": "no value"},  # malformed: skipped
            ],
        },
    },
}


class TestProtocol:

    def test_sends_initialize_and_returns_values_without_default(self):
        async def run():
            reader = asyncio.StreamReader()
            writer = FakeWriter()
            # A stray system line before the response must be skipped.
            _feed(reader, {"type": "system", "subtype": "hook"}, INIT_RESPONSE)
            values = await claude_models.read_model_values(reader, writer, timeout=2)
            return writer.lines, values

        lines, values = asyncio.run(run())
        assert lines[0]["type"] == "control_request"
        assert lines[0]["request"]["subtype"] == "initialize"
        # "default" is Orbital's empty setting (CLI default), not a choice.
        assert values == ["opus", "claude-fable-5-1[1m]", "sonnet", "haiku",
                          "claude-opus-5"]

    def test_error_response_raises(self):
        async def run():
            reader = asyncio.StreamReader()
            _feed(reader, {"type": "control_response", "response": {
                "subtype": "error", "request_id": "orbital-models",
                "error": "boom"}})
            return await claude_models.read_model_values(reader, FakeWriter(), timeout=2)

        with pytest.raises(RuntimeError):
            asyncio.run(run())

    def test_eof_raises(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_eof()
            return await claude_models.read_model_values(reader, FakeWriter(), timeout=2)

        with pytest.raises(RuntimeError):
            asyncio.run(run())


    def test_read_models_carries_display_names(self):
        async def run():
            reader = asyncio.StreamReader()
            _feed(reader, INIT_RESPONSE)
            return await claude_models.read_models(reader, FakeWriter(), timeout=2)

        models = asyncio.run(run())
        assert models[0] == {"value": "opus", "label": "Opus 5.5"}
        assert {"value": "claude-fable-5-1[1m]", "label": "Fable 5.1"} in models
        # An entry with no displayName still gets its value as the label.
        assert all(m["label"] for m in models)


class TestSpawn:

    def test_missing_binary_returns_none(self):
        assert asyncio.run(claude_models.fetch_claude_models(
            "/nonexistent/claude-binary", timeout=2)) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="posix shell script")
    def test_fake_cli_round_trip(self, tmp_path):
        script = tmp_path / "claude"
        payload = json.dumps(INIT_RESPONSE)
        script.write_text(
            "#!/bin/sh\n"
            "read line\n"
            f"printf '%s\\n' '{payload}'\n"
            "sleep 5\n")
        script.chmod(0o755)
        models = asyncio.run(claude_models.fetch_claude_models(str(script), timeout=5))
        assert models is not None
        assert "claude-fable-5-1[1m]" in [m["value"] for m in models]


class TestCache:

    def test_success_cached_until_cleared(self, monkeypatch):
        calls = []

        async def fake(binary="claude", **_kw):
            calls.append(binary)
            return [{"value": "opus", "label": "Opus 5.5"}]

        monkeypatch.setattr(claude_models, "fetch_claude_models", fake)
        claude_models.clear_claude_models_cache()
        try:
            want = [{"value": "opus", "label": "Opus 5.5"}]
            assert asyncio.run(claude_models.get_claude_models_cached("c")) == want
            assert asyncio.run(claude_models.get_claude_models_cached("c")) == want
            assert calls == ["c"]
            claude_models.clear_claude_models_cache()
            asyncio.run(claude_models.get_claude_models_cached("c"))
            assert calls == ["c", "c"]
        finally:
            claude_models.clear_claude_models_cache()
