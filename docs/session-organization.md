# Session organization

Fleet Dash organizes sessions by what the user should do next. Provider lifecycle
terms remain available for diagnosis, but they do not define the main page sections.

## Information model

Every session has three independent user-facing dimensions:

1. **Placement** answers: where should this session appear on the main page?
2. **Reason** answers: why is it there?
3. **Access and primary action** answer: what can Fleet do with it?

Every classification also exposes its winning rule, suppressed lower-priority rules, confidence
(`confirmed`, `inferred`, `stale`, or `unknown`), and ordered safe evidence facts. Expanded cards show
the current explanation. Full chat's **Why here?** rail adds the durable transition history.

The Now destination order is:

1. **Pinned**
2. **Action inbox**
3. **Working**
4. **Available**

The **Session history** placement follows those four live-inventory groups and owns inactive
inventory. Its flat chronological list renders in **Search under TYPE=SESSION** (the standalone
History destination is decommissioned; legacy `#history` links land there).

Pinned sessions are relocated, not duplicated. They retain their reason, access,
and primary action. An unpinned Needs-you session appears once as an inbox row, not
again as a full card. An unreviewed completed response also appears in the inbox
until it is opened or marked reviewed, then returns to the Available cards. Pinned
cards keep their inline interaction instead of duplicating an inbox row. Pins persist
in Fleet's server settings across browser reloads, daemon restarts, and devices.
Live Claude and Codex card headers execute navigation-only Open, Continue, and View actions, so
those cards do not render a duplicate button. Respond and Review remain explicit controls. Session
rows in Search TYPE=SESSION retain their action buttons because they use a separate row interaction.

Empty Pinned, Action inbox, and Working sections are hidden. Available remains visible
with an empty-state message. Search TYPE=SESSION shows one flat list or an explicit empty state;
session history is not a disclosure nested under Now.

The Action inbox is a presentation of normalized action records, not another lifecycle.
It includes provider-native questions, approvals, permissions, MCP forms, direct prose reply
requests, session errors requiring intervention, and completed work not yet reviewed. Every row has
a stable id derived from provider, session, kind, and pending nonce/conversation revision, so the same
request does not duplicate across refreshes or daemon restarts. Selecting it opens the existing full
session interaction. Bulk actions are restricted to mark reviewed, mark available, mute, and dismiss
reviewable completion notices. Unresolved questions/forms/approvals cannot be hidden by bulk triage,
and command/file approvals are never bulk actions.

## Placement and classification

| Placement | Classification |
| --- | --- |
| Pinned | The session id is in the persisted pin set. This placement overrides the normal section, but not its reason or access. |
| Needs you | A structured question, approval, permission, or MCP form is pending; the latest assistant prose directly requests a reply; a likely prompt could not be normalized; or a confirmed session-specific failure requires intervention. |
| Working | A provider reports an active turn, live turn evidence has no completion, compaction is active, or the turn is slow but not confirmed failed. External active turns retain the same Working reason while their access is labeled View only. |
| Available | No turn is active and nothing requires a response. This includes interactive provider `idle`, completed non-question turns, and recently active external/view-only sessions. |
| Session history | No turn is active and the session is dormant, reopenable, explicitly closed, or otherwise no longer part of the immediate inventory. External/view-only sessions become dormant after the configured inactivity threshold (2 hours by default); an externally archived Codex thread is removed from Fleet inventory immediately. |

Fleet incrementally observes at most the 32 most recently updated external Codex rollouts from the
last 24 hours, plus explicitly pinned external sessions. An active external session is Working and
an idle one is Available, both with View-only access. After the configured inactivity threshold
(2 hours by default) it becomes dormant and moves to Session history. If its final assistant prose
asks a direct question, it moves
to Needs you with View-only access instead. The exception is an unloaded external/view-only thread:
after 30 minutes of quiet, Fleet clears that prose-only request and moves it to Session history.

## Reason labels and actions

| Placement | Reason label | Classification details | Primary action |
| --- | --- | --- | --- |
| Needs you | Reply requested | Latest assistant prose contains a direct question or explicit response instruction outside code and quotations, and that conversation revision has not been dismissed. | Respond |
| Needs you | Question waiting | A provider-native structured question is pending. | Respond |
| Needs you | Command approval | A command-execution approval is pending. | Review |
| Needs you | File approval | A file-change approval is pending. | Review |
| Needs you | Permission needed | A provider permission request is pending. | Review |
| Needs you | Form waiting | An MCP elicitation form is pending. | Respond |
| Needs you | Response needed | The provider reports that the session is waiting, but Fleet has no more specific normalized request. For Claude, a bare registry flag must persist for 3 seconds; hook-captured questions and permissions are immediate. | Respond |
| Needs you | Check session | Claude appears frozen on an assistant tool prompt but the exact pending request was not captured. | Open |
| Needs you | Fix needed | The session has a confirmed provider or protocol error. | Open |
| Needs you | Limit reached | This session hit a provider rate, usage, quota, context, or token limit. Other sessions remain usable. | Open |
| Working | Compacting | Context compaction is active. | Open |
| Working | Working | A turn is active. External ownership is communicated separately by View-only access. | Open, or View when external |
| Working | Slow | The turn is still active but activity has exceeded the stall threshold. | Open, or View when external |
| Available | Available | No turn is active and nothing requires a response. External ownership is communicated separately by View-only access. | Continue, or View when external |
| Available | New response | A completed non-question assistant response has not been opened at its current conversation revision. Its placement remains Available, but the unreviewed outcome is presented in the Action inbox until reviewed. | Continue |
| Session history | Inactive | A managed, interactive session is dormant. | Continue |
| Session history | External | The external/view-only session has exceeded the configured inactivity threshold (2 hours by default). | View |
| Session history | Reopenable | The provider explicitly supports reopening the inactive session, or a closed Claude session still has its exact main transcript and original working directory. | Reopen; closed rows also retain View |
| Session history | Closed | A durable transcript remains but no safe reopen target is available. | View |

Opening a completed non-question response clears New response. Opening a
Reply-requested session does not clear the request. Reply requested clears only
when the user sends a response or explicitly chooses **Mark available**.

## Provider-state mapping

| Provider/raw state | User-facing result |
| --- | --- |
| `needs_you` plus a normalized question | Needs you / Question waiting |
| `needs_you` plus command approval | Needs you / Command approval |
| `needs_you` plus file approval | Needs you / File approval |
| `needs_you` plus permission approval | Needs you / Permission needed |
| `needs_you` plus MCP elicitation | Needs you / Form waiting |
| `needs_you` without normalized details | Needs you / Response needed; Claude's uncorroborated registry flag must persist for 3 seconds |
| Completed assistant prose requesting a response | Needs you / Reply requested |
| `stalled_or_prompt` | Needs you / Check session |
| `error` or a confirmed session-specific system error | Needs you / Fix needed |
| `blocked` or a recognized session-specific provider limit | Needs you / Limit reached |
| `running` | Working / Working |
| `running` plus external/view-only ownership | Working / Working / View only |
| Active compaction | Working / Compacting |
| `stalled` | Working / Slow |
| `idle` | Available / Available, including recently active external/view-only sessions |
| Completed-work `turn_done` handoff with concrete summary/verification | Available / Available, Action Inbox outcome: Unreviewed |
| Other non-question `turn_done` | Available / Available |
| External/view-only non-question `turn_done` | Available / Available / View only, optionally New response |
| Managed `dormant` | Session history / Inactive / Continue |
| Inactive `headless` or other read-only external thread beyond the configured inactivity threshold (2 hours by default) | Session history / External / View |
| `reopenable` | Session history / Reopenable / Reopen |
| Closed Claude ledger entry with a validated main transcript and cwd | Session history / Reopenable / View or Reopen |
| Explicitly closed ledger entry without a safe reopen target | Session history / Closed / View |
| `stale` caused by a provider-wide outage | Preserve the last known card placement and access; an owned interactive session keeps its controls, while an external session remains view-only; show one provider-level banner |

Provider-wide failures are page-level banners. Fleet must not duplicate the same
outage as a Fix-needed card for every session. A failure isolated to one session is
still Needs you / Fix needed. Stale confidence does not change runtime ownership: a transient
provider outage cannot turn a Fleet-owned session into View only.

## Reply detection

Reply detection examines the complete newest assistant prose, not the truncated card
peek. It ignores fenced code, inline code, Markdown quotations, and quoted strings.
A direct question counts when it is the final prose request. This prevents rhetorical or status
questions that the assistant immediately answers from creating false attention. Explicit response
instructions such as “answer both,” “choose one,” or “tell me which” also count even when later
background prose follows.

The dismissal record is keyed by session id and conversation revision. A later
assistant message therefore creates a new decision instead of inheriting an old
dismissal.

## Sorting

- Pinned: Needs you, Working, Available, History; newest activity breaks ties.
- Action inbox: approvals, questions/forms, problems, general attention, reply requests, then
  unreviewed outcomes; newest activity breaks ties within each kind.
- Working: stable entry order. A session appends at the bottom when it enters Working and keeps
  that position until it leaves Working. The order persists across browser reloads and daemon
  restarts; re-entering Working creates a new position at the bottom.
- Available: most recently active first.
- Session history: most recently active first.

## Session history controls

Session history is one flat chronological list. It does not recreate dormant,
external, reopenable, and closed subsections. It has one text search followed by two
filter rows:

- Access: All, Continue, View only, Reopen
- Provider: All, Claude, Codex

The access and provider filters combine. Each filter row is horizontally scrollable
on narrow screens. Results render 100 rows at a time so a full local transcript
archive does not overwhelm the page.

At daemon startup, Fleet indexes all surviving top-level Claude transcripts at
`~/.claude/projects/*/*.jsonl`. These imports include conversations that predate
Fleet. Nested saved-subagent transcripts are not separate sessions. Imported rows
always support View. They support Reopen only when the exact UUID transcript and
original working directory still exist and pass the server's containment checks.

## Visible versus diagnostic vocabulary

Card faces use the labels in this document. Raw provider state, CLI status, ownership,
and error details remain in expanded session information. The raw labels `idle`,
`turn_done`, `dormant`, `headless`, and `reopenable` are not main-page sections.

Claude Terminal remains a secondary control and may use the official background-job attach path.
Codex shows **Open** only for an exact already-running terminal; it never offers Attach or a disabled
terminal-state placeholder. The primary action describes the conversation action and must remain
truthful when no terminal exists.
