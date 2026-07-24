# Fleet Dash UI kit

Interactive recreation of the Now screen — the product's primary surface — composed entirely from the system components (`components/`).

What it demonstrates:
- Universal shell: 136px `NavRail` (usage footer, daemon dot) + queue pane + persistent right session workspace
- Needs-you cards: `QuestionCard` (summary only — ANSWER IN PANE →) and `ApprovalCard` (inline approve/deny)
- `SessionCard` meta-rail anatomy with a live `AgentRow` tree; click a card to open it on the right
- Workspace: `Tabs`, `ChatMessage` transcript, `QuestionDrawer` (pick an option → answered receipt), `Composer` (Cmd-Return sends, optimistic `Receipt`)

Not recreated here (see `Fleet Dash Redesign.dc.html` for those screens): Files/Agents/Details sections, Notifications, Search, Workstreams, Settings, Insights, mobile.
