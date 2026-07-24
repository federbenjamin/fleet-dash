import React from 'react';

/** Glyph square: ＋ ‹ › ⤡ ✕ ⋮ — text glyphs only, never SVG icons. */
export function IconButton({ glyph, size = 30, active, onClick, style }) {
  return (
    <button onClick={onClick} style={{
      width: size, height: size, flex: 'none', cursor: 'pointer',
      border: '1px solid ' + (active ? 'var(--amber-border)' : 'var(--line-input)'),
      borderRadius: 'var(--radius-control)', background: 'transparent',
      color: active ? 'var(--amber)' : 'var(--fg-dim-1)',
      font: "400 " + Math.round(size * .45) + "px 'Space Grotesk', sans-serif",
      display: 'flex', alignItems: 'center', justifyContent: 'center', ...style,
    }}>{glyph}</button>
  );
}
