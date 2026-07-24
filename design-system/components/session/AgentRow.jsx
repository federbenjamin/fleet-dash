import React from 'react';

/** 4-bar activity sparkline, deterministic per name. */
function Spark({ tone = 'green', seed = 0 }) {
  const h = [4, 8, 6, 10].map((v, i) => 3 + ((v + seed * (i + 3)) % 8));
  return (
    <div style={{ display: 'flex', gap: 2, alignItems: 'flex-end', height: 11, flex: 'none' }}>
      {h.map((x, i) => <div key={i} style={{ width: 3, height: x, background: `var(--${tone})` }} />)}
    </div>
  );
}

/** One subagent row: sparkline · name · — description · model · effort (right). Never truncate the description. */
export function AgentRow({ name, desc, model, tone = 'green', children, style }) {
  const seed = name ? name.length : 0;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, ...style }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Spark tone={tone} seed={seed} />
        <span style={{ font: "500 11px 'IBM Plex Mono', monospace", color: '#CFC8BA' }}>{name}</span>
        <span style={{ font: "400 11px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-2)' }}>— {desc}</span>
        <span style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--purple)', marginLeft: 'auto', whiteSpace: 'nowrap' }}>{model}</span>
      </div>
      {children && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginLeft: 4, borderLeft: '1px solid var(--line-soft)', paddingLeft: 12 }}>
          {children}
        </div>
      )}
    </div>
  );
}
