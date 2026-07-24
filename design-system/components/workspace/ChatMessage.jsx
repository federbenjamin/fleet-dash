import React from 'react';
import { Receipt } from '../core/Receipt.jsx';

/** Chat message. role 'user' = right warm bubble; 'agent' = left bubble with AUTHOR eyebrow; 'tool' = mono dim row. */
export function ChatMessage({ role = 'agent', author = 'CLAUDE', receipt, children, style }) {
  if (role === 'tool') {
    return <div style={{ font: "400 11px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)', padding: '2px 0', ...style }}>{children}</div>;
  }
  const user = role === 'user';
  return (
    <div style={{ alignSelf: user ? 'flex-end' : 'flex-start', maxWidth: user ? 380 : 460, display: 'flex', flexDirection: 'column', gap: 4, alignItems: user ? 'flex-end' : 'flex-start', ...style }}>
      {!user && <div style={{ font: "400 9px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)', letterSpacing: '.12em' }}>{author}</div>}
      <div style={{
        background: user ? 'var(--surface-raised)' : 'var(--surface-input)',
        border: '1px solid ' + (user ? 'var(--line-soft)' : 'var(--line)'),
        borderRadius: user ? '10px 10px 3px 10px' : '10px 10px 10px 3px',
        padding: '10px 14px', font: "400 13px 'Space Grotesk', sans-serif", color: '#CFC8BA', lineHeight: 1.45,
      }}>{children}</div>
      {receipt && <Receipt state={receipt} />}
    </div>
  );
}
