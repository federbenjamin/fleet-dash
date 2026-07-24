import React from 'react';

/** Workspace section tabs (CHAT FILES AGENTS DETAILS). Active = fg + 2px amber underline. */
export function Tabs({ items, active, onSelect, style }) {
  return (
    <div style={{ display: 'flex', gap: 14, font: "500 11px 'IBM Plex Mono', monospace", letterSpacing: '.08em', ...style }}>
      {items.map(t => (
        <span key={t} onClick={() => onSelect && onSelect(t)} style={{
          cursor: 'pointer', paddingBottom: 4,
          color: t === active ? 'var(--fg)' : 'var(--fg-dim-3)',
          borderBottom: t === active ? '2px solid var(--amber)' : '2px solid transparent',
        }}>{t}</span>
      ))}
    </div>
  );
}
