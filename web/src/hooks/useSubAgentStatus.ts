// Orbital — An operating system for AI agents
// Copyright (C) 2026 Orbital Contributors
// SPDX-License-Identifier: GPL-3.0-or-later

// The live sub-agent status of one chat session: fetch, refresh on every
// sub-agent lifecycle signal, light poll while anything is non-idle. Lifted
// out of SubAgentStatusBar (spec 099 §3.1) so the pin control's running dot
// reads the same pipeline instead of a second one. An agent with no live
// adapter is absent from the list, which means "not spawned" (idle).

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../config';
import { useWebSocket } from './useWebSocket';
import { isWorkerHandle } from '../utils/subAgentHandle';
import type { SubAgentRunStatus, WebSocketEvent } from '../types';

export interface SubAgentInfo {
  handle: string;
  display_name: string;
  status: SubAgentRunStatus;
  background_commands: string[];
}

export interface SubAgentStatusState {
  agents: SubAgentInfo[];
  refresh: () => Promise<void>;
}

export function useSubAgentStatus(
  projectId: string,
  sessionId: string | undefined,
  { enabled = true }: { enabled?: boolean } = {},
): SubAgentStatusState {
  const [agents, setAgents] = useState<SubAgentInfo[]>([]);
  const { on, off } = useWebSocket();
  const alive = useRef(true);

  const abortRef = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    // Bug #48 (fix C): a session switch re-fires this; abort the superseded
    // request instead of letting discarded responses pile up.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
      const data = await api<{ session_id: string | null; agents: SubAgentInfo[] }>(
        `/api/v2/agents/${projectId}/sub-agents/status${qs}`,
        { signal: controller.signal },
      );
      if (alive.current) {
        // Fanout workers (spec 009 §0.5) get their own live surface —
        // FanoutCard — and must NOT also show up as chips here.
        const next = (data?.agents ?? []).filter((a) => !isWorkerHandle(a.handle));
        // Identity-stable update: unchanged payloads keep the previous array
        // reference so effects keyed on state don't re-fire (and unstable
        // hook identities — e.g. test mocks recreating on/off per render —
        // cannot produce a render loop through this setState).
        setAgents((prev) =>
          JSON.stringify(prev) === JSON.stringify(next) ? prev : next,
        );
      }
    } catch {
      /* daemon may be restarting — status just goes quiet */
    }
  }, [projectId, sessionId]);

  // Refresh on every sub-agent lifecycle signal + light poll while non-idle.
  useEffect(() => {
    if (!enabled) return;
    alive.current = true;
    refresh();
    const handler = (e: WebSocketEvent) => {
      if ('project_id' in e && e.project_id && e.project_id !== projectId) return;
      refresh();
    };
    const events = [
      'sub_agent.started', 'sub_agent.completed', 'sub_agent.error',
      'sub_agent.failed', 'sub_agent.stopped', 'sub_agent.turn_interrupted',
      'chat.sub_agent_message', 'agent.status',
    ] as const;
    events.forEach((ev) => on(ev, handler));
    return () => {
      alive.current = false;
      abortRef.current?.abort();
      events.forEach((ev) => off(ev, handler));
    };
  }, [enabled, projectId, on, off, refresh]);

  useEffect(() => {
    if (!enabled) return;
    const anyActive = agents.some((a) => a.status !== 'idle');
    if (!anyActive) return;
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [enabled, agents, refresh]);

  return { agents, refresh };
}
