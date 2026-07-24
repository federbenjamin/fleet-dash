import React from 'react';
import { UsageBar } from '../core/UsageBar.jsx';
import { StatusDot } from '../core/StatusDot.jsx';

/** Fixed 136px nav rail. items: [{label, badge?}]. usage: per-window rows for the footer. */
export function NavRail({ items = [], active, onSelect, usage = [], user = 'you', daemonOk = true, style }) {
  return (
    <div style={{ width: 'var(--w-rail)', flex: 'none', borderRight: '1px solid var(--line)', display: 'flex', flexDirection: 'column', padding: '18px 10px 14px', gap: 4, boxSizing: 'border-box', ...style }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 8px 14px' }}>
        <div style={{ width: 22, height: 22, flex: 'none', border: '1px solid var(--amber-border)', borderRadius: 'var(--radius-badge)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--amber)', font: "600 11px 'Space Grotesk', sans-serif" }}>F</div>
        <span style={{ font: "600 12px 'Space Grotesk', sans-serif" }}>Fleet Dash</span>
      </div>
      {items.map(it => {
        const isActive = it.label === active;
        return (
          <div key={it.label} onClick={() => onSelect && onSelect(it.label)} style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 10px', cursor: 'pointer',
            font: "500 11px 'IBM Plex Mono', monospace", letterSpacing: '.08em',
            background: isActive ? 'var(--surface-raised)' : 'transparent',
            border: '1px solid ' + (isActive ? 'var(--line-input)' : 'transparent'),
            borderRadius: 'var(--radius-control)', color: isActive ? 'var(--fg)' : 'var(--fg-dim-3)',
          }}>
            {it.label}
            {it.badge != null && <span style={{ color: 'var(--amber)' }}>{it.badge}</span>}
          </div>
        );
      })}
      <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ borderTop: '1px solid var(--line)', paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 7 }}>
          {usage.map((u, i) => (
            <div key={i}>
              <div style={{ display: 'flex', justifyContent: 'space-between', font: "400 9px 'IBM Plex Mono', monospace", color: u.pct >= 90 ? 'var(--red)' : u.pct >= 70 ? 'var(--amber)' : 'var(--fg-dim-1)', marginBottom: 3 }}>
                <span>{u.label}</span><span>{u.pct}%</span>
              </div>
              <UsageBar pct={u.pct} color={u.pct < 70 ? 'var(--green)' : undefined} />
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <div style={{ width: 18, height: 18, flex: 'none', borderRadius: '50%', background: 'var(--surface-raised)', border: '1px solid var(--line-input)', display: 'flex', alignItems: 'center', justifyContent: 'center', font: "600 8px 'Space Grotesk', sans-serif", color: 'var(--fg-dim-1)' }}>{user.slice(0, 1).toUpperCase()}</div>
          <span style={{ font: "400 9.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>{user}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, font: "400 9px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>
          <StatusDot tone={daemonOk ? 'green' : 'red'} size={6} />local daemon
        </div>
      </div>
    </div>
  );
}
