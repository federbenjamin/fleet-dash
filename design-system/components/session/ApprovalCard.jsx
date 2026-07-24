import React from 'react';
import { Button } from '../core/Button.jsx';

/** Needs-you APPROVAL card — unlike questions, approvals keep their inline action buttons. */
export function ApprovalCard({ session, meta, command, quiet, onApproveOnce, onApproveSession, onDeny, style }) {
  return (
    <div style={{ background: 'var(--surface-raised)', border: '1px solid var(--amber-border)', borderRadius: 9, padding: '11px 13px', display: 'flex', flexDirection: 'column', gap: 7, ...style }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ font: "600 9.5px 'IBM Plex Mono', monospace", color: 'var(--amber)', letterSpacing: '.08em' }}>◆ PERMISSION — APPROVAL</span>
        {quiet && <span style={{ font: "400 9.5px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-3)' }}>{quiet}</span>}
      </div>
      <div style={{ font: "600 13px 'Space Grotesk', sans-serif" }}>{session}</div>
      {meta && <div style={{ font: "400 10px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-1)' }}>{meta}</div>}
      <div style={{ font: "400 11px 'IBM Plex Mono', monospace", color: 'var(--fg-dim-2)', background: 'var(--surface-input)', border: '1px solid var(--line)', borderRadius: 'var(--radius-control)', padding: '7px 10px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{command}</div>
      <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
        <Button small onClick={onApproveOnce}>Approve once</Button>
        <Button small onClick={onApproveSession}>Approve for session</Button>
        <Button small variant="danger" onClick={onDeny}>Deny</Button>
      </div>
    </div>
  );
}
