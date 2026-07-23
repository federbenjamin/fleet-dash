# Send reliability and fullscreen question-panel regression contract

Status: Implemented and verified on `fix/active-usage-warning` for draft PR #10.

Date: 2026-07-18

## Production evidence

The affected Fleet-owned Codex session is
`codex:019f6bb5-1a72-7041-a7f2-afab571271d9`. The production daemon accepted the HTTP action,
then Codex rejected `turn/steer` with `no active turn to steer`. Fleet rendered the optimistic user
message as failed while the same stale turn record continued to render **Main session working**.

The fullscreen question panel is rebuilt by the two-second fleet poll. Its nested scroll position is
not restored, so a person reading a long multi-part question is repeatedly returned to its top.

## Required behavior

### SR-1 — A stale active-turn record cannot lose a message

- A definitive `no active turn to steer` rejection means the old turn did not accept the message.
- Fleet clears that stale turn authority and retries the same payload once with `turn/start`.
- Text and local-image payloads follow the same transition.
- If Codex reports a different active turn instead, Fleet must not start a concurrent turn. It queues
  the exact payload under its existing idempotency key until control becomes safe.
- An ambiguous timeout never triggers an automatic direct retry.

### SR-2 — “Working” requires current work evidence

- A direct provider response that no active turn exists invalidates the corresponding local running
  state immediately.
- A later `turn/started` notification or successful `turn/start` may establish working state again.
- Transcript visibility and stale thread metadata never manufacture turn authority.

### QP-1 — Polls never move the reader

- The question drawer and its independent scroll position survive every fleet poll and unrelated
  status update.
- Choosing an option, moving between question parts, and expanding status details preserve the
  question's intended position unless the user explicitly changes question pages.
- Active drag gestures are never interrupted by a render.

### QP-2 — The question drawer is vertically resizable

- A visible horizontal grip at the top of the question drawer supports mouse, trackpad, and touch.
- Dragging upward can expand the drawer to the usable area immediately below the chat title bar.
- Dragging downward snaps to a compact waiting-question bar. Tapping that bar expands it again.
- The current question remains answerable at every non-collapsed size.
- Height and collapsed state are stored per session and question nonce on the current device.
- Size is clamped after viewport or orientation changes; it can never push the composer off-screen.
- Keyboard focus, safe-area insets, reduced motion, and touch scrolling remain supported.

## Verification gates

1. Protocol unit: stale `no active turn` clears authority and becomes a safe new turn.
2. Protocol unit: different-turn mismatch is queueable and never starts another turn.
3. Adapter unit: failed stale steer retries text and image payloads exactly once.
4. Adapter state unit: provider rejection cannot leave the session classified as working.
5. Desktop browser: question scroll position survives at least three fleet poll renders.
6. Mobile browser: the same scroll test passes at 390×844.
7. Desktop and mobile browser: drag expands, shrinks, snaps closed, and reopens.
8. Existing optimistic delivery, offline queue, question answering, composer, and activity-footer
   suites remain green.

Production is not changed until PR #10 is merged and the production deployment script passes its
API and Web Push health gates.

## Verification evidence

- 269/269 Python tests pass, including protocol rejection classification, exact text/image restart,
  different-turn queueing, and stale-Working suppression.
- 11/11 Node service-worker and Web Push privacy tests pass.
- The new drawer check passes on desktop and 390×844 mobile inside the full matrix and in repeated
  focused runs. It forces three poll replacements while scrolled, selects an option at the bottom,
  drags to the title bar, verifies the composer stays visible, snaps closed, reloads, restores the
  previous expanded height, and confirms the explicit Answer action reopens it.
- The full browser matrix has 137 applicable checks: 134 passed in the serialized 12-minute run,
  with one expected project-specific skip. Three unrelated mobile checks timed out under that run's
  load; all three passed twice immediately in isolation (6/6).
- Final desktop/mobile screenshots were inspected. The mobile expanded drawer reaches directly below
  the title bar while the status strip and canonical composer remain reachable.
