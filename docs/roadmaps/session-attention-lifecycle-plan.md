# Session Attention Lifecycle Plan

## Execution status

Implemented on `fix/session-attention-lifecycle`. PR #76 had already merged
before its terminal-error dormancy correction could be added, so that
append-only correction is included on this branch with the rest of the batch.
The affected Python suites, focused desktop/mobile browser regressions, Web Push
tests, and three-lane audit pass. The full test runs retain unrelated timing
flakes documented in the PR verification notes.

## Problem

`Needs You` currently conflates an unresolved condition with a session that still
deserves primary queue placement. Old conditions can therefore live forever.
The observed Quirk-Safe Codex thread was more than 16 days quiet with registry
status `notLoaded`, but its retained `blocked` state alternated between `Needs
You` after a successful detail refresh and `Available` when that refresh was
deferred and the row became `stale`.

The root failure is precedence: historical attention state can outrank strong
inactive evidence, while the stale fallback does not reproduce the successful
refresh result. Manual dismissal is also currently an inbox-only operation; it
does not change session placement.

## Decisions

### 1. Inactivity wins over historical attention

A session with no active-work evidence moves to `History` after the existing
`dormant_seconds` threshold (two hours by default), even if its last recorded
state was `blocked`, `error`, or pending attention. This changes placement only;
it does not close, archive, kill, or delete the provider session.

Active work remains authoritative. Age must not demote a loaded or owned active
turn, a running tool, compaction, or a current native prompt with live evidence.

Alternative considered: keep every unresolved condition in `Needs You` until it
is explicitly resolved. That preserves visibility but recreates the immortal
queue item. It is rejected because queue placement should express current
urgency, not permanent unresolved history.

### 2. Stale projections decay consistently

`stale` is a refresh-quality wrapper, not a lifecycle destination. When retained
provider evidence proves the session inactive and its quiet age exceeds the
dormancy threshold, stale placement is `History`. Otherwise stale projections
preserve their previous normalized lifecycle state and group; they must never
escalate urgency from incomplete evidence.

Alternative considered: always preserve the prior group while stale. That avoids
flicker but freezes an old `Needs You` classification indefinitely during
deferred refreshes. It is rejected because time and retained `notLoaded`
evidence are sufficient to make a safe downward transition.

### 3. Dismissal suppresses one exact attention item

Manual dismissal records the server-owned identity of the current attention
item. A still-live session then moves from `Needs You` to `Available`, while a
yellow dot continues to show that the underlying condition is unresolved. A
new question, permission, error, or revision gets a new identity and resurfaces
normally. If the session is already dormant, it remains in `History`.

Dismissal is UI triage only. It must not send Escape, answer a native prompt,
interrupt a turn, mutate the transcript, or erase the condition.

Alternative considered: dismiss the whole session until any activity occurs.
That is broader and can hide a new request that happens to arrive before other
activity. Exact identity suppression is the right boundary because it retains a
backstop without weakening new-attention delivery.

### 4. History is a one-click Search mode

Search gets a visible `History` command that selects the existing session result
type and all-access scope while preserving the current query, provider, and
project filters. Results use the existing chronological session list,
pagination, and session actions; no second history implementation is created.

Alternative considered: restore a standalone History destination. That would
duplicate navigation, filtering, rendering, and pagination. The Search shortcut
restores one-click access while keeping one canonical implementation.

## Implementation

### Existing PR correction

Update draft PR #76 so terminal Claude usage/credit failures initially classify
as `error`, but can still age into dormant placement after `dormant_seconds`
when there is no active-work evidence. Add regression coverage before pushing a
follow-up commit to that branch.

### Provider normalization and placement

- In Claude scanning, let confirmed inactivity and quiet age demote terminal
  provider errors instead of exempting them forever.
- In Codex normalization, evaluate proven `notLoaded` dormancy before retained
  historical blocked/error/pending state, without overriding active-turn
  evidence.
- In provider-neutral placement, make stale rows use retained lifecycle and
  inactivity evidence consistently. A stale inactive row may decay to History;
  a stale row without that proof retains its prior placement.
- Keep `docs/session-organization.md` synchronized with the new precedence.

### Exact dismissal

- Reuse the existing server-owned action identity and durable
  `dismissed_actions` setting.
- Expose dismissal for individual actionable Needs You records through the
  existing authenticated triage mutation path.
- Feed exact dismissal state into placement. Suppress only that attention
  candidate, not dormant placement or the underlying provider state.
- Project an explicit dismissed-but-unresolved flag for browser rendering.
- Render the surviving indicator with the existing amber Needs You color. The
  amber dot is the visual signature; no new palette or animation is introduced.

### Search History control

- Add a deliberate one-click History command to the existing Search controls.
- Selecting it switches to session results with all access states and preserves
  other useful filters.
- Reuse the current Console typography, command-chip geometry, and focus states.
- Bump the service-worker shell cache for changed static assets.

### Documentation

Update `README.md` for the user-visible dismissal and History access behavior.

## Acceptance criteria

1. A `notLoaded` Codex session quiet beyond two hours appears in History on both
   successful-detail and deferred/stale scans, including when its retained state
   is blocked or error.
2. A terminal Claude usage/credit error appears in Needs You while recent, then
   moves to History after two quiet hours without active work.
3. Active, waiting, running, or compacting sessions are not demoted merely due
   to transcript age.
4. Dismissing the current attention item moves a live session to Available and
   leaves an amber unresolved dot.
5. The same dismissed identity stays suppressed across polls and restart; a new
   identity returns the session to Needs You.
6. Dismissal never sends native input and never pulls a dormant session out of
   History.
7. Search exposes History in one click and shows the existing session history
   list without clearing query, provider, or project filters.
8. Relevant Python and browser tests pass, followed by independent correctness,
   scope-completeness, and missed-surface audits.

## Out of scope

- Automatically closing, archiving, interrupting, or deleting sessions.
- Treating dismissal as a provider-native prompt action.
- Adding a separate History page or duplicating the Search session renderer.
- Changing the two-hour default dormancy threshold.
