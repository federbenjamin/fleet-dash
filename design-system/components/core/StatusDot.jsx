import React from 'react';

/** tone: amber | green | red | blue | purple | dim */
export function StatusDot({ tone = 'green', size = 7, style }) {
  const bg = tone === 'dim' ? 'var(--fg-dim-3)' : `var(--${tone})`;
  return <div style={{ width: size, height: size, flex: 'none', borderRadius: '50%', background: bg, ...style }} />;
}
