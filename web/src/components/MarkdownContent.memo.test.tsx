// Orbital — An operating system for AI agents
// Copyright (C) 2026 Orbital Contributors
// SPDX-License-Identifier: GPL-3.0-or-later

// Spec 100 §3.4.8: MarkdownContent is memoized, so a parent re-render with
// the same props does not re-parse the markdown. The spy counts renders of
// the real react-markdown component.

import { fireEvent, render } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

const parses = { count: 0 };
vi.mock('react-markdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-markdown')>();
  const { createElement } = await import('react');
  const Real = actual.default;
  function CountingMarkdown(props: Parameters<typeof Real>[0]) {
    parses.count += 1;
    return createElement(Real, props);
  }
  return { ...actual, default: CountingMarkdown };
});

import MarkdownContent from './MarkdownContent';

const onOpenPath = () => {};

function Parent({ content }: { content: string }) {
  const [tick, setTick] = useState(0);
  return (
    <div>
      <button type="button" onClick={() => setTick((n) => n + 1)}>
        tick {tick}
      </button>
      <MarkdownContent content={content} workspace="/w" onOpenPath={onOpenPath} />
    </div>
  );
}

describe('MarkdownContent memo', () => {
  it('skips re-parsing when only the parent re-renders', () => {
    const { getByRole, rerender } = render(<Parent content="**hello**" />);
    expect(parses.count).toBe(1);

    fireEvent.click(getByRole('button'));
    fireEvent.click(getByRole('button'));
    expect(getByRole('button').textContent).toBe('tick 2');
    expect(parses.count).toBe(1);

    rerender(<Parent content="**changed**" />);
    expect(parses.count).toBe(2);
  });
});
