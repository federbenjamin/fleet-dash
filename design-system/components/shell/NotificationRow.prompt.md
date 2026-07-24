Notification event row. Tapping opens a thin actions sub-card attached below (snooze 15m/1h/tomorrow · mute · mark read) — LOCKED: actions never appear as a bar on the chat pane.

```jsx
<NotificationRow title="design-probe asked a question" meta="2m" unread open={openId===id} onTap={...} onAction={...} />
```