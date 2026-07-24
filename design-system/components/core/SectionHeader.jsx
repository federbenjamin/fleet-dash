import React from 'react';
import { StatusDot } from './StatusDot.jsx';

/** Queue section header: 7px dot + UPPERCASE mono label + · count. tone 'dim' drops the dot. */
export function SectionHeader({ label, count, tone = 'green', style }) {
  const color = tone === 'dim' ? 'var(--fg-dim-3)' : `var(--${tone})`;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, font: "600 10px 'IBM Plex Mono', monospace", letterSpacing: '.16em', color, ...style }}>
      {tone !== 'dim' && <StatusDot tone={tone} size={7} />}
      {label}{count != null && ` · ${count}`}
    </div>
  );
}
