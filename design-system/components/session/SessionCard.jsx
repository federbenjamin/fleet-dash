import React from 'react';
import { UsageBar } from '../core/UsageBar.jsx';
import { StatusDot } from '../core/StatusDot.jsx';

const tones = { working: 'green', needs: 'amber', stalled: 'red', paused: 'red', limit: 'red', viewonly: 'dim', closed: 'dim' };
const labels = { working: 'working', needs: 'needs you', stalled: 'stalled', paused: 'paused', limit: 'limit', viewonly: 'view-only', closed: 'closed' };

/** Desktop session card with an adaptive right meta rail; mobile puts meta inline. */
export function SessionCard({ title, repo, peek, status = 'working', model, ctx, quiet, agentCount, selected, pinned, mobile, onClick, children, style }) {
  const tone = tones[status] || 'green';
  const alert = status === 'stalled' || status === 'limit';
  const statusColor = tone === 'dim' ? 'var(--fg-dim-3)' : `var(--${tone})`;
  const cardBorder = selected ? 'var(--amber-border)' : status === 'needs' ? 'var(--amber-border)' : 'var(--line)';
  const cardBg = status === 'needs' ? 'var(--amber-surface)' : 'var(--surface-card)';
  const metaFont = { font: "400 10.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' };
  const head = (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
        {pinned && <span style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--amber)' }}>⌖</span>}
        <div style={{ font: "600 14px 'Space Grotesk', sans-serif", whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{title}</div>
      </div>
      <div style={{ font: "400 11px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>{repo}</div>
    </div>
  );
  const peekEl = peek && (
    <div style={{ font: "400 11.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-2)', lineHeight: 1.55, borderTop: '1px solid #1E1B15', paddingTop: 8 }}>{peek}</div>
  );
  if (mobile) {
    return (
      <div onClick={onClick} style={{ background: cardBg, border: `1px solid ${cardBorder}`, borderRadius: 'var(--radius-card)', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8, cursor: onClick ? 'pointer' : 'default', ...style }}>
        {head}
        <div style={{ display: 'flex', gap: 9, ...metaFont }}>
          <span style={{ color: statusColor, fontWeight: 600 }}>{labels[status]}</span>
          {model && <span style={{ color: 'var(--purple)' }}>{model}</span>}
          {ctx != null && <span>ctx {ctx}%</span>}
          {quiet && <span style={{ color: alert ? 'var(--red)' : 'var(--fg-dim-3)' }}>quiet {quiet}</span>}
        </div>
        {peekEl}{children}
      </div>
    );
  }
  return (
    <div onClick={onClick} style={{ background: cardBg, border: `1px solid ${cardBorder}`, borderRadius: 'var(--radius-card)', display: 'flex', overflow: 'hidden', cursor: onClick ? 'pointer' : 'default', boxShadow: selected ? '0 0 0 1px var(--amber-border)' : 'none', ...style }}>
      <div style={{ flex: 1, padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
        {head}{peekEl}{children}
      </div>
      <div style={{ width: 'var(--w-meta-rail)', flex: 'none', borderLeft: '1px solid #1E1B15', background: alert ? '#150E0C' : '#12100C', padding: 12, display: 'flex', flexDirection: 'column', gap: 7, ...metaFont }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <StatusDot tone={tone} size={6} />
          <span style={{ color: statusColor, fontWeight: 600 }}>{labels[status]}</span>
        </div>
        {model && <div style={{ color: 'var(--purple)' }}>{model}</div>}
        {ctx != null && <div>ctx {ctx}%<UsageBar pct={ctx} style={{ marginTop: 4 }} /></div>}
        {quiet && <div style={{ color: alert ? 'var(--red)' : 'inherit' }}>quiet {quiet}</div>}
        {agentCount != null && <div style={{ color: 'var(--green)' }}>{agentCount} agents</div>}
      </div>
    </div>
  );
}
