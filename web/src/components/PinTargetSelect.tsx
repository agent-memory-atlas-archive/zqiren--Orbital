// Orbital — An operating system for AI agents
// Copyright (C) 2026 Orbital Contributors
// SPDX-License-Identifier: GPL-3.0-or-later

// Spec 074 — the composer pin control. The sticky selection IS the session's
// sub-agent pin: choosing a worker PATCHes `pinned_target`, and while pinned
// every composer send dispatches straight to that worker with zero
// management-agent turns. Selecting Orbital (the manager) back is the unpin.
//
// Design (aligned 2026-08-31 via the "Composer Pin Header" mockup): a
// permanent logo-only mark fused to the input row's left edge — Orbital's own
// mark at rest, the pinned agent's mark while pinned. Colorless chrome: no
// tint, no name; identity comes from the avatar itself, the tooltip, and the
// "Message {agent}…" placeholder. Hidden entirely when no sub-agents are
// installed: zero surface for users without workers.
//
// Spec 099 (approved mockup, 2026-09-27): each row carries an availability
// dot and, for claude-code / codex, what is left of the subscription's
// 5-hour window (weekly when tighter). A hover card beside the menu shows
// every window with reset times; phones get both windows stacked in the row
// instead. The collapsed control stays colorless — it only gains a corner
// dot when the pinned worker needs attention (low, limited, not ready,
// running).

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';
import MessageAvatar from './MessageAvatar';
import { useT } from '../i18n/useT';
import { useLocale } from '../i18n/LocaleContext';
import {
  refreshAgentAvailability,
  useAgentAvailability,
} from '../hooks/useAgentAvailability';
import {
  ageParts,
  formatResetTime,
  leftTone,
  relativeParts,
  statusDot,
  summarizeQuota,
  windowLeft,
  type DotColor,
  type QuotaTone,
  type ShownWindow,
} from '../utils/quotaDisplay';
import type { AgentAvailability, QuotaWindowKind } from '../types';

/** Result of resolving one composer send against the session pin. */
export interface ResolvedSendTarget {
  /** Slug to dispatch to, or undefined for the management agent. */
  target?: string;
  /** Message text, always verbatim — a leading `@slug` is ordinary text. */
  content: string;
  /** True whenever `target` is set: the sticky dropdown pin is the composer's
   *  only direct-to-worker send (spec 091). The backend maps every `target`
   *  send to initiator="user_pinned" — the wake-suppressed dispatch class. */
  pinned: boolean;
}

/**
 * Target resolution (spec 091, replacing spec 074 §3.2's @mention
 * precedence): the sticky pin applies, otherwise the management agent. There
 * is no `@` parse. `@codex do X` sent to Orbital is an ordinary request for
 * Orbital to dispatch (its supervised path); sent while pinned, it reaches
 * the pinned worker as typed. Switching who you talk to is the dropdown.
 */
export function resolveSendTarget(
  text: string,
  pinnedTarget: string | null | undefined,
): ResolvedSendTarget {
  if (pinnedTarget) {
    return { target: pinnedTarget, content: text, pinned: true };
  }
  return { target: undefined, content: text, pinned: false };
}

interface PinTargetSelectProps {
  /** Installed sub-agents (App-level `/agents/available`, built-in excluded). */
  agents: Array<{ slug: string; name: string }>;
  /** Currently pinned slug, or null when talking to Orbital (the manager). */
  value: string | null;
  /** Fired with the new slug, or null when Orbital is selected (unpin). */
  onChange: (slug: string | null) => void;
  disabled?: boolean;
  /**
   * Spec 079 — where this control is mounted.
   *
   * `'composer'` (the default, and ChatView's only spelling) keeps the
   * edge-fusion geometry described above: negative margins that pull the mark
   * through the composer card's padding, a full-height left-rounded button
   * with a divider on its right, and a menu that opens upward off a control
   * sitting at the bottom of the viewport.
   *
   * `'standalone'` is the same menu as an ordinary inline form control — a
   * self-contained bordered pill that claims no space it wasn't given. Used by
   * the queue composer's option row and the automation form, neither of which
   * has a card edge to fuse to.
   */
  variant?: 'composer' | 'standalone';
  /** Accessible name + tooltip for the manager (unassigned) state. Defaults to
   *  the chat pin's wording; the queue and automation surfaces say "runs this"
   *  rather than "talking to". */
  managerLabel?: string;
  /** Spec 099: slugs with an open turn in this session (from the shared
   *  useSubAgentStatus pipeline) — their dot pulses green. */
  runningSlugs?: readonly string[];
  'data-testid'?: string;
}

const MENU_ROW =
  'w-full flex items-center gap-2.5 px-3 py-2 text-[13px] text-primary text-left ' +
  'hover:bg-card-hover/50 max-md:min-h-[52px]';

const ELEVATED = 'bg-card border border-border rounded-[10px] ' +
  'shadow-[0_10px_28px_rgba(24,24,27,0.10),0_2px_6px_rgba(24,24,27,0.05)]';

/** Agents whose subscription quota Orbital can read (spec 099 §2.1). */
const QUOTA_SLUGS = new Set(['claude-code', 'codex']);

type T = ReturnType<typeof useT>;

// ---------------------------------------------------------------------------
// Dot
// ---------------------------------------------------------------------------

const DOT_FILL: Record<DotColor, string> = {
  green: 'bg-success text-success',
  amber: 'bg-warning text-warning',
  red: 'bg-error text-error',
  grey: 'bg-card shadow-[inset_0_0_0_1.5px_var(--color-muted)]',
};

const DOT_LABEL = {
  green: 'pinAgent.status.running',
  amber: 'pinAgent.status.low',
  red: 'pinAgent.status.limited',
  grey: 'pinAgent.status.needsLogin',
} as const;

function StatusDot({ color, pulse, testId, t }: {
  color: DotColor; pulse: boolean; testId: string; t: T;
}) {
  return (
    <span
      data-testid={testId}
      data-color={color}
      data-pulse={pulse ? 'true' : 'false'}
      role="img"
      aria-label={t(DOT_LABEL[color])}
      className={`absolute -right-[3px] -bottom-[3px] w-[9px] h-[9px] rounded-full
                  border-2 border-card box-content ${DOT_FILL[color]}`}
    >
      {pulse && (
        <span
          aria-hidden
          className="absolute -inset-[3px] rounded-full border-2 border-current
                     motion-safe:animate-ping [animation-duration:1.6s] motion-reduce:opacity-40"
        />
      )}
    </span>
  );
}

function AvatarWithDot({ slug, dot, dotTestId, t }: {
  slug?: string;
  dot: { color: DotColor; pulse: boolean } | null;
  dotTestId: string;
  t: T;
}) {
  return (
    <div className="relative shrink-0">
      <MessageAvatar variant="agent" agentHandle={slug} />
      {dot && <StatusDot {...dot} testId={dotTestId} t={t} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row usage text
// ---------------------------------------------------------------------------

type LineTone = 'ok' | 'low' | 'out' | 'muted';
interface Line { text: string; tone: LineTone }

const LINE_CLASS: Record<LineTone, string> = {
  ok: 'text-secondary',
  low: 'text-[#B45309]',
  out: 'text-[#DC2626]',
  muted: 'text-muted',
};

function windowText(w: ShownWindow, t: T): string {
  return t('pinAgent.quota.left', {
    window: t(w.kind === 'weekly' ? 'pinAgent.quota.window.weekly' : 'pinAgent.quota.window.fiveHour'),
    n: w.left,
  });
}

/** [top line, optional second line]: the desktop row shows the first, a
 *  phone row stacks both (no hover to hide the other window behind). */
function usageLines(
  slug: string, info: AgentAvailability | undefined, now: number,
  t: T, locale: 'en' | 'zh',
): Line[] {
  if (!info) return [];
  if (info.ready === false) return [{ text: t('pinAgent.status.needsLogin'), tone: 'ok' }];
  if (!QUOTA_SLUGS.has(slug)) return [];
  const s = summarizeQuota(info.quota, now);
  if (s.kind === 'none') return [{ text: t('pinAgent.quota.noReading'), tone: 'muted' }];
  if (s.kind === 'stale') return [{ text: t('pinAgent.quota.noCurrent'), tone: 'muted' }];
  const second = (o: ShownWindow | null): Line[] =>
    o ? [{ text: windowText(o, t), tone: 'muted' }] : [];
  if (s.kind === 'limited') {
    const text = s.resetsAt
      ? t('pinAgent.quota.limitReached', { time: formatResetTime(s.resetsAt, now, locale) })
      : t('pinAgent.quota.limitReachedNoTime');
    return [{ text, tone: 'out' }, ...second(s.other)];
  }
  return [{ text: windowText(s.shown, t), tone: s.tone }, ...second(s.other)];
}

// ---------------------------------------------------------------------------
// Hover card
// ---------------------------------------------------------------------------

const CARD_WINDOWS: Array<[QuotaWindowKind, Parameters<T>[0]]> = [
  ['five_hour', 'pinAgent.card.window.fiveHour'],
  ['weekly', 'pinAgent.card.window.weekly'],
  ['weekly_opus', 'pinAgent.card.window.weeklyOpus'],
  ['weekly_sonnet', 'pinAgent.card.window.weeklySonnet'],
];

const BAR_FILL: Record<QuotaTone, string> = {
  ok: 'bg-accent', low: 'bg-warning', out: 'bg-error',
};

const titleCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function unitText(unit: 'minutes' | 'hours' | 'days', n: number, prefix: 'age' | 'in', t: T) {
  return t(`pinAgent.quota.${prefix}.${unit}` as Parameters<T>[0], { n });
}

function hasCard(slug: string | null, info: AgentAvailability | undefined): boolean {
  if (!slug || !info) return false;
  return info.ready === false || QUOTA_SLUGS.has(slug) || slug === 'cursor';
}

function QuotaCard({ slug, name, info, now, t, locale }: {
  slug: string; name: string; info: AgentAvailability; now: number;
  t: T; locale: 'en' | 'zh';
}) {
  const shell = (header: ReactNode, body: ReactNode) => (
    <div
      role="tooltip"
      data-testid="pin-quota-card"
      className={`${ELEVATED} w-[300px] px-3.5 py-3 flex flex-col gap-2.5 text-left`}
    >
      <div className="flex items-center gap-2">{header}</div>
      {body}
    </div>
  );
  const note = (text: string) => <p className="m-0 text-xs text-secondary">{text}</p>;

  if (info.ready === false) {
    return shell(
      <>
        <AvatarWithDot slug={slug} dot={{ color: 'grey', pulse: false }} dotTestId="pin-card-dot" t={t} />
        <b className="text-[13px] font-semibold">{name}</b>
      </>,
      note(t('pinAgent.card.needsLogin', { name })),
    );
  }
  if (!QUOTA_SLUGS.has(slug)) {
    return shell(
      <>
        <MessageAvatar variant="agent" agentHandle={slug} />
        <b className="text-[13px] font-semibold">{name}</b>
      </>,
      note(t('pinAgent.card.cursor')),
    );
  }

  const quota = info.quota;
  const header = (
    <>
      <MessageAvatar variant="agent" agentHandle={slug} />
      <b className="text-[13px] font-semibold">{t('pinAgent.card.usage', { name })}</b>
      {quota?.plan && (
        <span className="ml-auto text-[11px] text-muted">
          {t('pinAgent.card.plan', { plan: titleCase(quota.plan) })}
        </span>
      )}
    </>
  );
  if (!quota) {
    return shell(header, note(t(slug === 'codex'
      ? 'pinAgent.card.noReadingCodex' : 'pinAgent.card.noReadingClaude')));
  }

  const blocks = CARD_WINDOWS.flatMap(([kind, labelKey]) => {
    const w = quota.windows?.find((x) => x.kind === kind);
    if (!w) return [];
    const left = windowLeft(w, now);
    const resets = w.resets_at ? new Date(w.resets_at) : null;
    const time = resets ? formatResetTime(resets, now, locale) : null;
    let sub: string;
    if (left === null) sub = t('pinAgent.card.void');
    else if (left <= 0 && time) sub = t('pinAgent.card.reached', { time });
    else if (time && resets) {
      const rel = relativeParts(resets, now);
      sub = rel
        ? t('pinAgent.card.remainRel', { n: left, time, rel: unitText(rel.unit, rel.n, 'in', t) })
        : t('pinAgent.card.remain', { n: left, time });
    } else sub = t('pinAgent.card.remainNoReset', { n: left });
    const tone = left === null ? 'ok' : leftTone(left);
    return [(
      <div key={kind} className="flex flex-col gap-1">
        <div className="flex justify-between gap-2 text-xs">
          <b className="font-medium">{t(labelKey)}</b>
          <span className="text-secondary tabular-nums">{left === null ? '—' : `${left}%`}</span>
        </div>
        <div className="h-1.5 rounded-[3px] bg-card-hover overflow-hidden">
          <i
            className={`block h-full rounded-[3px] ${BAR_FILL[tone]}`}
            style={{ width: `${left ?? 0}%` }}
          />
        </div>
        <div className="text-[11px] text-muted tabular-nums">{sub}</div>
      </div>
    )];
  });

  const age = ageParts(quota.observed_at, now);
  const fromReply = slug === 'claude-code';
  const footer = age === null ? null : age.unit === 'now'
    ? t(fromReply ? 'pinAgent.card.updatedNowReply' : 'pinAgent.card.updatedNow')
    : t(fromReply ? 'pinAgent.card.updatedAgoReply' : 'pinAgent.card.updatedAgo',
      { ago: unitText(age.unit, age.n, 'age', t) });

  return shell(header, (
    <>
      {blocks}
      {footer && (
        <div className="text-[11px] text-muted border-t border-border pt-2">{footer}</div>
      )}
    </>
  ));
}

/** Date.now(), re-read every 30 s while `active` (ages, reset voiding). */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, [active]);
  return active ? now : Date.now();
}

export default function PinTargetSelect({
  agents,
  value,
  onChange,
  disabled,
  variant = 'composer',
  managerLabel,
  runningSlugs,
  'data-testid': testId,
}: PinTargetSelectProps) {
  const t = useT();
  const { locale } = useLocale();
  const [open, setOpen] = useState(false);
  // Whose hover card shows while the menu is open (defaults to the pinned
  // worker's), and whether the collapsed control is hovered.
  const [cardFor, setCardFor] = useState<string | null>(null);
  const [controlHover, setControlHover] = useState(false);
  const availability = useAgentAvailability();
  const now = useNow(open || controlHover);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocMouseDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  // No sub-agents installed → no pin surface at all.
  if (agents.length === 0) return null;

  // A stale pin (agent uninstalled since) still renders so the user can see
  // and clear it — the avatar lookup falls back to a monogram badge, and the
  // menu appends a bare-slug row instead of silently showing Orbital while
  // the session is actually still pinned.
  const known = agents.some((a) => a.slug === value);
  const pinnedName = value ? (agents.find((a) => a.slug === value)?.name ?? value) : null;

  const pick = (slug: string | null) => {
    setOpen(false);
    onChange(slug);
  };

  const toggle = () => {
    // Decided from the rendered `open`, never inside a setState updater
    // (updaters must stay pure; CLAUDE.md React anti-patterns).
    if (!open) {
      setCardFor(value);
      setControlHover(false);
      // One backend-cached REST hit; also kicks the codex read-through.
      refreshAgentAvailability();
    }
    setOpen(!open);
  };

  const standalone = variant === 'standalone';
  const managerText = managerLabel ?? t('pinAgent.tooltipManager');
  const running = new Set(runningSlugs ?? []);
  const dotFor = (slug: string) => statusDot({
    ready: availability[slug]?.ready,
    running: running.has(slug),
    quota: QUOTA_SLUGS.has(slug) ? availability[slug]?.quota : undefined,
    now,
  });
  const nameOf = (slug: string) => agents.find((a) => a.slug === slug)?.name ?? slug;
  const card = (slug: string | null) => (slug && hasCard(slug, availability[slug]) ? (
    <QuotaCard
      slug={slug}
      name={nameOf(slug)}
      info={availability[slug]}
      now={now}
      t={t}
      locale={locale}
    />
  ) : null);
  const pinnedHasCard = hasCard(value, value ? availability[value] : undefined);
  const controlDot = value ? dotFor(value) : null;
  const menuCard = open ? card(cardFor) : null;
  const hoverCard = !open && controlHover ? card(value) : null;

  return (
    // Composer: -ml-3/-my-2 pull the control through the composer card's
    // padding so the mark fuses to the card's left edge, full row height
    // (self-stretch), per the aligned mockup. rounded-l matches the card's
    // inner radius. Standalone drops all of that and sits inline.
    <div
      ref={rootRef}
      className={
        standalone
          ? 'relative flex shrink-0'
          : 'relative self-stretch flex shrink-0 -ml-3 -my-2'
      }
      data-testid={testId ?? 'pin-target-select'}
      data-tour="pin-select"
    >
      <button
        type="button"
        onClick={toggle}
        onMouseEnter={() => setControlHover(true)}
        onMouseLeave={() => setControlHover(false)}
        disabled={disabled}
        aria-label={managerLabel ?? t('pinAgent.aria')}
        aria-haspopup="listbox"
        aria-expanded={open}
        // The usage card replaces the native tooltip when there is one; two
        // tooltips on one hover would stack.
        title={pinnedHasCard ? undefined : pinnedName
          ? t('pinAgent.tooltipPinned', { name: pinnedName })
          : managerText}
        className={
          standalone
            ? `flex items-center gap-1 px-1.5 py-1 rounded-md border border-border
               hover:bg-card-hover/50 disabled:opacity-50 focus-visible:outline-none
               focus-visible:bg-card-hover/50 max-md:min-h-[36px]`
            : `flex items-center gap-1 pl-3 pr-1.5 rounded-l-[7px] border-r border-border
               hover:bg-card-hover/50 disabled:opacity-50 focus-visible:outline-none
               focus-visible:bg-card-hover/50 max-md:min-w-[48px]`
        }
      >
        <AvatarWithDot
          slug={value ?? undefined}
          dot={controlDot}
          dotTestId="pin-control-dot"
          t={t}
        />
        <ChevronDown size={10} className="text-muted shrink-0" />
      </button>

      {hoverCard && (
        <div
          className={`absolute ${standalone ? 'top-full mt-1' : 'bottom-full mb-2'}
                     left-0 z-50 max-md:hidden pointer-events-none`}
        >
          {hoverCard}
        </div>
      )}

      {open && (
        // Menu and hover card share one positioned wrapper so the card sits
        // beside the menu, bottom-aligned with it (top-aligned when the
        // standalone menu opens downward).
        <div
          className={`absolute ${standalone ? 'top-full mt-1 items-start' : 'bottom-full mb-2 items-end'}
                     left-0 z-50 flex gap-2`}
        >
          <div
            role="listbox"
            aria-label={managerLabel ?? t('pinAgent.aria')}
            // Standalone opens downward: it is an ordinary field in the middle of
            // a form, where an upward menu would clip against the surface above.
            className={`${ELEVATED} min-w-[300px] max-w-[calc(100vw-2rem)] overflow-hidden`}
          >
            <button
              type="button"
              role="option"
              aria-selected={value === null}
              onClick={() => pick(null)}
              onMouseEnter={() => setCardFor(null)}
              onFocus={() => setCardFor(null)}
              className={MENU_ROW}
            >
              <MessageAvatar variant="agent" />
              <span className="font-medium whitespace-nowrap">{t('pinAgent.orbital')}</span>
              <span className="ml-auto flex items-center gap-2 text-xs text-muted whitespace-nowrap">
                <span>{t('pinAgent.managerRole')}</span>
                <span className="w-3 text-center">{value === null ? '✓' : ''}</span>
              </span>
            </button>
            <div className="h-px bg-border/60" aria-hidden />
            {agents.map((a) => {
              const lines = usageLines(a.slug, availability[a.slug], now, t, locale);
              return (
                <button
                  key={a.slug}
                  type="button"
                  role="option"
                  aria-selected={value === a.slug}
                  onClick={() => pick(a.slug)}
                  onMouseEnter={() => setCardFor(a.slug)}
                  onFocus={() => setCardFor(a.slug)}
                  className={`${MENU_ROW} ${cardFor === a.slug && menuCard ? 'md:bg-card-hover/50' : ''}`}
                >
                  <AvatarWithDot
                    slug={a.slug}
                    dot={dotFor(a.slug)}
                    dotTestId={`pin-dot-${a.slug}`}
                    t={t}
                  />
                  <span className="font-medium whitespace-nowrap">{a.name}</span>
                  <span className="ml-auto flex items-center gap-2 text-xs text-muted whitespace-nowrap tabular-nums">
                    {lines[0] && (
                      <span
                        data-testid={`pin-quota-${a.slug}`}
                        data-tone={lines[0].tone}
                        className={`max-md:hidden ${LINE_CLASS[lines[0].tone]}`}
                      >
                        {lines[0].text}
                      </span>
                    )}
                    {lines[0] && (
                      <span
                        data-testid={`pin-quota-stack-${a.slug}`}
                        className="hidden max-md:flex flex-col items-end gap-px"
                      >
                        {lines.map((l, i) => (
                          <span key={i} className={LINE_CLASS[l.tone]}>{l.text}</span>
                        ))}
                      </span>
                    )}
                    <span className="w-3 text-center">{value === a.slug ? '✓' : ''}</span>
                  </span>
                </button>
              );
            })}
            {value && !known && (
              <button
                type="button"
                role="option"
                aria-selected
                onClick={() => pick(value)}
                onMouseEnter={() => setCardFor(null)}
                className={MENU_ROW}
              >
                <MessageAvatar variant="agent" agentHandle={value} />
                <span className="font-medium">{value}</span>
                <span className="ml-auto flex items-center gap-2 text-xs text-muted">
                  <span className="w-3 text-center">✓</span>
                </span>
              </button>
            )}
          </div>
          {menuCard && <div className="max-md:hidden">{menuCard}</div>}
        </div>
      )}
    </div>
  );
}
