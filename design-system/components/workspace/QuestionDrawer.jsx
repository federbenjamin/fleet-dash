import React from 'react';

/** Pending-question drawer — the amber-register bottom drawer above the composer.
    THE canonical place to answer questions; option selectors never appear on queue cards. */
export function QuestionDrawer({ title, question, options = [], page, onPick, onOther, collapsed, onToggle, answered, style }) {
  return (
    <div style={{ borderTop: '1px solid var(--amber-border)', background: 'var(--amber-surface)', ...style }}>
      <div onClick={onToggle} style={{ display: 'flex', justifyContent: 'center', padding: 5, cursor: onToggle ? 'pointer' : 'default' }}>
        <div style={{ width: 36, height: 3, borderRadius: 2, background: 'var(--line-input)' }} />
      </div>
      <div style={{ padding: '2px 20px 14px', display: 'flex', flexDirection: 'column', gap: 9 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ font: "600 11px 'IBM Plex Mono', monospace", color: 'var(--amber)', letterSpacing: '.1em' }}>{title}</span>
          {page && <span style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)' }}>{page} · {collapsed ? 'EXPAND ▴' : 'COLLAPSE ▾'}</span>}
        </div>
        {!collapsed && <>
          <div style={{ font: "500 13.5px 'Space Grotesk', sans-serif" }}>{question}</div>
          {answered ? (
            <div style={{ font: "400 10.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>answered: <span style={{ color: 'var(--green)' }}>{answered} ✓</span> — awaiting transcript confirmation</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {options.map((o, i) => (
                <div key={i} onClick={() => onPick && onPick(o)} style={{
                  border: '1px solid ' + (o.rec ? 'var(--amber)' : 'var(--line-input)'),
                  background: o.rec ? 'var(--amber-tint)' : 'transparent',
                  borderRadius: 'var(--radius-option)', padding: '9px 12px', cursor: 'pointer',
                }}>
                  <div style={{ font: (o.rec ? '600' : '500') + " 12px 'Space Grotesk', sans-serif", color: o.rec ? 'var(--fg)' : '#CFC8BA' }}>
                    {o.label}{o.rec && <span style={{ font: "400 9.5px 'IBM Plex Mono', monospace", color: 'var(--amber)' }}> · recommended</span>}
                  </div>
                  {o.desc && <div style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)', marginTop: 2 }}>{o.desc}</div>}
                </div>
              ))}
              <div onClick={onOther} style={{ border: '1px dashed var(--line-input)', borderRadius: 'var(--radius-option)', padding: '9px 12px', font: "500 12px 'Space Grotesk', sans-serif", color: 'var(--fg-dim-1)', cursor: 'pointer' }}>Other — type a custom answer…</div>
            </div>
          )}
        </>}
      </div>
    </div>
  );
}
