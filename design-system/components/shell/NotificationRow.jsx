import React from 'react';
import { StatusDot } from '../core/StatusDot.jsx';

/** Notification event row. When open, a thin actions sub-card attaches below the row —
    actions NEVER appear as a bar on the chat pane. */
export function NotificationRow({ title, meta, tone = 'amber', unread, open, onTap, onAction, style }) {
  const actions = ['SNOOZE 15M', 'SNOOZE 1H', 'TOMORROW', 'MUTE', 'MARK READ'];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', ...style }}>
      <div onClick={onTap} style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '11px 14px', cursor: 'pointer',
        background: open ? 'var(--surface-raised)' : 'var(--surface-card)',
        border: '1px solid ' + (open ? 'var(--line-input)' : 'var(--line)'),
        borderRadius: open ? '9px 9px 0 0' : 9,
      }}>
        <StatusDot tone={unread ? tone : 'dim'} size={6} />
        <span style={{ font: (unread ? '600' : '400') + " 12.5px 'Space Grotesk', sans-serif", color: unread ? 'var(--fg)' : 'var(--fg-dim-1)' }}>{title}</span>
        <span style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)', marginLeft: 'auto', whiteSpace: 'nowrap' }}>{meta}</span>
      </div>
      {open && (
        <div style={{ display: 'flex', gap: 6, padding: '8px 14px', background: 'var(--surface-input)', border: '1px solid var(--line-input)', borderTop: 'none', borderRadius: '0 0 9px 9px', overflowX: 'auto' }}>
          {actions.map(a => (
            <span key={a} onClick={() => onAction && onAction(a)} style={{ font: "500 9.5px 'IBM Plex Mono', monospace", letterSpacing: '.05em', color: 'var(--fg-dim-1)', border: '1px solid var(--line-input)', borderRadius: 'var(--radius-chip)', padding: '5px 9px', cursor: 'pointer', whiteSpace: 'nowrap' }}>{a}</span>
          ))}
        </div>
      )}
    </div>
  );
}
