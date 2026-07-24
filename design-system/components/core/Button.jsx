import React from 'react';

const base = {
  font: "600 12px 'Space Grotesk', sans-serif", borderRadius: 'var(--radius-control)',
  padding: '8px 14px', cursor: 'pointer', background: 'transparent',
  border: '1px solid var(--line-input)', color: 'var(--fg)', whiteSpace: 'nowrap',
};
const variants = {
  primary: { background: 'var(--btn-primary-bg)', color: 'var(--btn-primary-fg)', border: '1px solid var(--btn-primary-bg)' },
  secondary: {},
  amber: { border: '1px solid var(--amber-border)', color: 'var(--amber)' },
  danger: { border: '1px solid var(--red-border)', color: 'var(--red)' },
};

export function Button({ variant = 'secondary', small, wide, disabled, onClick, children, style }) {
  return (
    <button onClick={disabled ? undefined : onClick} style={{
      ...base, ...variants[variant] || {},
      ...(small ? { padding: '6px 12px', font: "600 11.5px 'Space Grotesk', sans-serif" } : {}),
      ...(wide ? { width: '100%', textAlign: 'center' } : {}),
      ...(disabled ? { opacity: .45, cursor: 'default' } : {}), ...style,
    }}>{children}</button>
  );
}
