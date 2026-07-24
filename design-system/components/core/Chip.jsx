import React from 'react';

/** Filter/config chip. active = filled dark surface; tone colors the label (e.g. purple for models). */
export function Chip({ active, tone, onClick, children, style }) {
  return (
    <span onClick={onClick} style={{
      display: 'inline-block', border: '1px solid ' + (active ? 'var(--line-input)' : 'var(--line)'),
      background: active ? 'var(--surface-raised)' : 'transparent',
      borderRadius: 'var(--radius-badge)', padding: '6px 12px', whiteSpace: 'nowrap',
      font: "500 10.5px 'IBM Plex Mono', monospace", letterSpacing: '.05em',
      color: tone ? `var(--${tone})` : active ? 'var(--fg)' : 'var(--fg-dim-3)',
      cursor: onClick ? 'pointer' : 'default', ...style,
    }}>{children}</span>
  );
}
