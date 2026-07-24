import React from 'react';

const copy = {
  delivered: 'delivered ✓',
  sending: 'sending — awaiting transcript confirmation',
  queued: 'queued · waiting for session',
  offline: 'Queued offline — will send when the daemon is reachable',
};

/** Delivery receipt line under a sent message. Honest states only. */
export function Receipt({ state = 'delivered', text, style }) {
  const color = state === 'offline' ? 'var(--amber)' : 'var(--fg-dim-3)';
  return (
    <div style={{ font: "400 9.5px 'IBM Plex Mono', monospace", color, display: 'flex', alignItems: 'center', gap: 6, ...style }}>
      {state === 'sending' && <span style={{ display: 'inline-block', width: 8, height: 8, border: '1px solid var(--fg-dim-3)', borderTopColor: 'transparent', borderRadius: '50%', animation: 'fd-spin 1s linear infinite' }} />}
      {text || copy[state]}
      <style>{'@keyframes fd-spin{to{transform:rotate(360deg)}}'}</style>
    </div>
  );
}
