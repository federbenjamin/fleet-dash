import React from 'react';

/** Needs-you QUESTION card for the Now queue. Shows ONLY the question + summary line —
    option selectors live exclusively in the right-pane drawer. */
export function QuestionCard({ session, meta, heading = 'QUESTION WAITING', question, optionCount, recommended, quiet, onOpen, style }) {
  return (
    <div onClick={onOpen} style={{ background: 'var(--surface-raised)', border: '1px solid var(--amber-border)', borderRadius: 9, padding: '11px 13px', display: 'flex', flexDirection: 'column', gap: 5, cursor: onOpen ? 'pointer' : 'default', ...style }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ font: "600 9.5px 'IBM Plex Mono', monospace", color: 'var(--amber)', letterSpacing: '.08em' }}>◆ {heading}</span>
        {quiet && <span style={{ font: "400 9.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)' }}>{quiet}</span>}
      </div>
      <div style={{ font: "600 13px 'Space Grotesk', sans-serif" }}>{session}</div>
      {meta && <div style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>{meta}</div>}
      <div style={{ font: "500 13px 'Space Grotesk', sans-serif", color: 'var(--fg)', lineHeight: 1.4 }}>{question}</div>
      <div style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>
        {optionCount} options · recommended: {recommended} — <span style={{ color: 'var(--amber)' }}>ANSWER IN PANE →</span>
      </div>
    </div>
  );
}
