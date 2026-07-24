import React from 'react';
import { IconButton } from '../core/IconButton.jsx';
import { Button } from '../core/Button.jsx';

/** Chat composer: ＋ | input | Send. Return = newline, Cmd/Ctrl-Return = send.
    Composer text can never accidentally answer a native question. */
export function Composer({ value, onChange, onSend, placeholder = 'Message the session…', disabled, style }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', ...style }}>
      <IconButton glyph="＋" size={34} />
      <textarea
        value={value} disabled={disabled} placeholder={placeholder}
        onChange={e => onChange && onChange(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); onSend && onSend(); } }}
        rows={1}
        style={{
          flex: 1, resize: 'none', background: 'var(--surface-input)', border: '1px solid var(--line-input)',
          borderRadius: 'var(--radius-control)', padding: '9px 12px', font: "400 13px 'Space Grotesk', sans-serif",
          color: 'var(--fg)', outline: 'none', minHeight: 16, fontSize: 13,
        }} />
      <Button variant="primary" onClick={onSend} disabled={disabled} style={{ padding: '9px 16px' }}>Send</Button>
    </div>
  );
}
