The canonical Now-queue session card. Desktop: content left + locked 118px meta rail (status/model/ctx/quiet/agents). Mobile: pass `mobile` for inline status. Nest `<AgentRow>` children for live subagent trees.

```jsx
<SessionCard title="Explain the three needs of judgments" repo="quirk · refactor/offset-op-lock"
  status="working" model="opus-4-8" ctx={28} quiet="7s" agentCount={4}
  peek="All gates green. Committing C on the follow-up branch…">
  <AgentRow name="explore" desc="scanning fixture server routes" model="haiku · low" />
</SessionCard>
```

status drives everything: needs → amber border/surface; stalled/limit → red register + darker rail.