import React from 'react';

/** 3px usage bar. Fill auto-escalates: blue < 70 ≤ amber < 90 ≤ red. Pass color to override. */
export function UsageBar({ pct = 0, color, height = 3, style }) {
  const fill = color || (pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--amber)' : 'var(--blue)');
  return (
    <div style={{ height, background: 'var(--line)', borderRadius: 'var(--radius-bar)', ...style }}>
      <div style={{ width: Math.min(100, pct) + '%', height: '100%', background: fill, borderRadius: 'var(--radius-bar)' }} />
    </div>
  );
}
