# Session organization

Fleet Dash organizes sessions by what the user should do next. Provider lifecycle
terms remain available for diagnosis, but they do not define the main page sections.

## Information model

Every session has three independent user-facing dimensions:

1. **Placement** answers: where should this session appear on the main page?
2. **Reason** answers: why is it there?
3. **Access and primary action** answer: what can Fleet do with it?

The Now destination order is:

1. **Pinned**
2. **Needs you**
3. **Working**
4. **Available**

The separate **History** destination follows those four live-inventory groups and owns inactive
inventory.

Pinned sessions are relocated, not duplicated. They retain their reason, access,
and primary action. Pinned cards sort by the same urgency order as the main page,
then by newest activity. Pins persist in Fleet's server settings across browser
reloads, daemon restarts, and devices.

Empty Pinned, Needs you, and Working sections are hidden. Available remains visible
with an empty-state message. History shows one flat list or an explicit empty state; it is not a
disclosure nested under Now.

## Placement and classification

| Placement | Classification |
| --- | --- |
| Pinned | The session id is in the persisted pin set. This placement overrides the normal section, but not its reason or access. |
| Needs you | A structured question, approval, permission, or MCP form is pending; the latest assistant prose directly requests a reply; a likely prompt could not be normalized; or a confirmed session-specific failure requires intervention. |
| Working | A provider reports an active turn, live turn evidence has no completion, compaction is active, or the turn is slow but not confirmed failed. External active turns are Working even though Fleet cannot control them. |
| Available | No turn is active, Fleet can submit another message, and nothing requires a response. This includes provider `idle` and completed non-question turns. |
| Session history | No turn is active and the session is dormant, external/view-only, reopenable, explicitly closed, or otherwise no longer part of the immediate interactive inventory. |

An inactive external session moves to Session history immediately. If it starts a
turn again, it returns to Working. If its final assistant prose asks a direct
question, it moves to Needs you with View-only access instead.

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
| Working | Compacting | Context compaction is active. | Open |
| Working | Working | Fleet owns an active turn. | Open |
| Working | Working elsewhere | An external provider runtime owns an active turn. | View |
| Working | Slow | The turn is still active but activity has exceeded the stall threshold. | Open, or View when external |
| Available | Available | The session is interactive, has no active turn, and has no reply request. | Continue |
| Available | New response | A completed non-question assistant response has not been opened at its current conversation revision. This is a secondary badge; the placement remains Available. | Continue |
| Session history | Inactive | A managed, interactive session is dormant. | Continue |
| Session history | External | The external/view-only session has no active turn. | View |
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
| `running` | Working / Working |
| `running` plus external/view-only ownership | Working / Working elsewhere |
| Active compaction | Working / Compacting |
| `stalled` | Working / Slow |
| `idle` | Available / Available |
| Non-question `turn_done` | Available / Available, optionally New response |
| Managed `dormant` | Session history / Inactive / Continue |
| Inactive `headless` or other read-only external thread | Session history / External / View |
| `reopenable` | Session history / Reopenable / Reopen |
| Closed Claude ledger entry with a validated main transcript and cwd | Session history / Reopenable / View or Reopen |
| Explicitly closed ledger entry without a safe reopen target | Session history / Closed / View |
| `stale` caused by a provider-wide outage | Preserve the last known card placement and show one provider-level banner |

Provider-wide failures are page-level banners. Fleet must not duplicate the same
outage as a Fix-needed card for every session. A failure isolated to one session is
still Needs you / Fix needed.

## Reply detection

Reply detection examines the complete newest assistant prose, not the truncated card
peek. It ignores fenced code, inline code, Markdown quotations, and quoted strings.
Questions anywhere in the remaining prose count, including multi-part interview
questions followed by later explanatory text. Explicit instructions such as “answer
both,” “choose one,” or “tell me which” also count.

The dismissal record is keyed by session id and conversation revision. A later
assistant message therefore creates a new decision instead of inheriting an old
dismissal.

## Sorting

- Pinned: Needs you, Working, Available, History; newest activity breaks ties.
- Needs you: longest waiting first.
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

Terminal Open/Attach remains a secondary control. The primary action describes the
conversation action and must remain truthful when the terminal is unavailable.
