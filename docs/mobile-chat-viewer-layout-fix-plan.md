# Mobile chat + Markdown viewer layout fix plan

Status: implementation and automated verification complete; staging/prod promotion in progress.
Branch: `fix/send-now-or-queue`
Release policy: isolated staging first; user approved production promotion after the release gates.

This is the durable source of truth for the 2026-07-19 mobile chat/Markdown viewer correction.
Every requirement stays open until its source fix, deterministic desktop/mobile regression, complete
automated gate, and real-iPhone staging check are recorded here.

## Product and layout contract

- Product: Fleet Dash's full-screen session chat and received-file Markdown viewer.
- Audience: one operator reading and steering long-running Claude/Codex work from an iPhone.
- Job: maximize stable reading space while keeping one compact, predictable composer and one compact
  context/action strip immediately above it.
- Visual direction: retain Fleet's existing dark operational-console language. Chat and Markdown
  already share the canonical `renderComposer`; preserve it and make its one three-part control
  strip—`＋ | message | Send`—structurally and geometrically exact on both surfaces. Do not replace it
  with two new look-alike implementations.
- Geometry rule: composer controls align by one compact height and baseline. Markdown's upper
  file-browser/Chat row is deliberately taller so its file chips keep their normal tap area. Chat's
  latest-file button matches the compressed status height, does not stretch when status expands,
  and remains bottom-aligned with the expanded status.
  Safe-area padding exists only where iOS requires it; it must never become visible dead space
  between Fleet and the keyboard.

## Reference evidence

| Reference | Expected capture | Status |
| --- | --- | --- |
| P1 | Full-screen chat, composer not focused | Waived; reproduce from source and staging |
| P2 | Full-screen chat, composer focused with iOS keyboard visible | Waived; reproduce with mobile visual-viewport harness and staging |
| P3 | Markdown viewer | Waived; reproduce from source and staging |

## Verified source findings

These are verified against current source, not inferred from prior plan status.

| Finding | Current-source evidence | Required correction |
| --- | --- | --- |
| One shared composer exists, but it is not geometrically canonical. | `renderComposer` emits a two-row field and viewer-only Chat stack (`static/app.js:903-914`); textarea, Plus, and Send use conflicting 40/42/44px rules (`static/fleet.css:568-592`, `static/fleet.css:1393-1397`). | Keep the shared renderer; emit one identical `＋ \| textarea \| Send` row with one height token and bounded newline growth. |
| The keyboard seam accumulates several independent gaps. | The dock adds `10px + safe-area-inset-bottom` (`static/fleet.css:876-877`); the keyboard override depends on a focus-plus-80px heuristic (`static/app.js:2581-2602`); always-rendered image/feedback rows carry margins below the controls (`static/fleet.css:571`, `static/fleet.css:595`). | Put attachments/feedback above the control row, collapse empty rows, and derive the keyboard seam from the shortened visual viewport rather than focus timing alone. |
| Keyboard resize has no reading anchor. | Focus/blur toggles `.composer-active`, the mobile rule hides the upper context, viewport sync directly rewrites overlay geometry, and then calls `scrollIntoView` (`static/app.js:1911-1920`, `static/app.js:2581-2633`, `static/fleet.css:1391`). | Capture bottom intent or the first visible body block before every mutation, restore it after layout, and remove composer `scrollIntoView`. |
| Full-screen titles need a stable action-relative anchor. | Both headers were asymmetric flex rows (`dashboard.html:149-164`, `static/fleet.css:781-796`); Markdown title text was only 12.5px. A first repair viewport-centered the title, but staging feedback requires it directly after the X. | Use `X | flexible left-aligned title | actions`, vertically centered controls, and one larger title token. |
| File ordering is newest-first only until a path is delivered again. | Claude updates an existing deque entry in place (`engine.py:1175-1183`); Codex overwrites an insertion-ordered map entry in place (`codex_adapter.py:2825-2856`). Both context responses otherwise reverse into newest-first order. | Move a repeated file path to the newest position provider-side; render `files[0]` as Chat's latest-file action. |
| Failed automatic-send receipts resurrect. | `reconcileOutboxOptimistic` recreates every unresolved server Outbox receipt (`static/app.js:720-779`); the failed receipt exposes Restore only and has no durable dismissal tombstone (`static/app.js:1957-1982`). | Persist bounded resolved Outbox IDs. Restore and Dismiss both tombstone the source; Restore alone returns exact text/images. Uncertain delivery gets Dismiss but no blind retry. |
| Receipt-only changes can claim bottom authority. | Optimistic status/error is part of the body render key and optimistic inserts explicitly scroll to the bottom (`static/app.js:1881-1892`, `static/app.js:3049-3058`, `static/app.js:3103-3143`, `static/app.js:4589-4602`). | Separate canonical follow-tail from optimistic receipt repainting; failed/resurrected receipts never advance it. |
| Production Photo loses the trusted iOS tap; current branch contains only the first half of the repair. | Production closes/blurs the menu during `pointerdown` before native picker activation; current `openImagePicker` now clicks first (`static/app.js:883-899`) but still relies on a synthetic `.click()`. Image persistence/upload/send is at `static/app.js:4482-4578`. | Make the actual file input the trusted menu tap target, cover cancel and both surfaces, then verify on Mobile Safari staging. |
| Temporarily missing sessions still break image queueing. | `store_image_upload` rejects when the session is missing or temporarily lacks submit/queue capability (`engine.py:1690-1710`), before the durable when-available send path can own the image. | Admit uploads only for a validated live-or-ledger-known session, then let the existing idempotent Outbox path own and queue the normalized image. |
| Follow-tail is a fragile 12px render-time check. | `renderSession` recomputes `atBottom` from a 12px threshold and performs one post-composer animation-frame pin (`static/app.js:3103-3143`); it has no durable user intent or late-growth observer. | Track near-bottom intent explicitly, disengage on a deliberate upward scroll, and re-pin after canonical and late content/layout growth. |
| Composer Send can bypass a visible native question. | `sendText` routes ordinary text independently of `pendingQuestion`, so a stale provider projection can inject text while the native selector is still mounted (`static/app.js:4662-4710`). | Send one nonce-bound `dismiss_then_send` action. The server must accept the exact dismiss first, then durably queue the follow-up; it must never type during the question-to-chat transition. |

## Requirements tracker

| ID | Requirement | Acceptance check | Status |
| --- | --- | --- | --- |
| MCV-01 | The message field is too tall on both surfaces. Its resting height must exactly match the Send button. | Computed resting heights match on chat and Markdown at mobile and desktop widths; multiline behavior follows D1 below. | Implemented; focused browser pass |
| MCV-02 | When the iOS keyboard is visible, remove the gap between the keyboard and Fleet's composer. | The composer's bottom edge is flush with the visual viewport/keyboard edge except for one intentional small design margin; no page background or underlying Now screen is visible. | Implemented; focused mobile pass |
| MCV-03 | Move `＋` to the left of the message field on both surfaces. | Both render in the exact order `＋ | message field | Send`; keyboard and pointer tab order match the visual order. | Implemented; focused browser pass |
| MCV-04 | Chat and Markdown must use the exact same `＋`, message field, and Send components. | Both surfaces call the same renderer and share the same DOM classes, sizing tokens, draft/image state, menu behavior, send path, focus behavior, and feedback states. No duplicated look-alike markup remains. | Implemented; focused browser pass |
| MCV-05 | In Markdown, move **Chat** to the right of the file browser. Make both tall enough for the file chips and reduce the browser width to fit. | The upper action strip is `file browser | Chat`; both controls are an identical 42px, the chips fill that height, the browser flexes without clipping useful identity, and Chat stays reachable at 390px width. | Implemented; tightened desktop + mobile pass |
| MCV-06 | Both page titles are too small and should sit directly to the right of the X button. | Chat and Markdown titles use one larger type token in `X | title | actions` grids; title text starts one grid gap after X, truncates before right actions, and all controls remain vertically centered and tappable. | Implemented; desktop + mobile correction pass |
| MCV-07 | A failed sent-message receipt persists in chat, repeatedly forces the history to the bottom, and cannot be removed. | A failed/expired optimistic receipt never participates in canonical bottom anchoring. It exposes the settled D3 recovery behavior, can be removed, and never reappears after reload once resolved. | Implemented; focused reload pass |
| MCV-08 | Remove the file browser from chat. Put a button for the most recently received file to the right of the status section. | Chat's upper action strip is `status line | latest-file button`. Compressed status is 44px with its expansion chevron inside the summary. The button is absent when there is no received file, opens the newest received file by canonical delivery order, exactly matches that compressed height, stays fixed when status expands, and keeps its bottom edge aligned to the expanded status. The full file browser exists only in Markdown. | Implemented; tightened mobile expansion pass |
| MCV-09 | Reduce excess space above the header and below the composer to maximize body height. | Header top inset and composer bottom inset equal the minimum safe-area-aware tokens on iPhone portrait/landscape; no content touches the sensor/status or home indicator; chat/file body gains the reclaimed height. | Implemented; focused mobile geometry pass |
| MCV-10 | Opening/closing the keyboard must move the body with the composer instead of changing which part of the chat/file is visible. | On focus and blur, the body resizes in the same frame as the visual viewport and preserves the D2 reading anchor. There is no jump to another message/file paragraph and no delayed corrective snap. | Implemented; focused chat + Markdown pass |
| MCV-11 | The `＋` → **Photo** action does not work in production. | A trusted tap keeps the menu action alive through the native picker, accepts supported phone images, shows the selected image draft, survives navigation/reload, and sends or queues it exactly once through the owning provider. Cancel cleanly changes nothing. The same action works from chat and Markdown because both use MCV-04's canonical composer. | Implemented; browser + transient-session unit pass |
| MCV-12 | Chat does not reliably follow new replies when already at or near the bottom, and can leave the newest reply partly cut off. | Opening chat starts in follow-tail mode. Remaining within the documented near-bottom threshold keeps follow-tail active across canonical message arrivals, composer/status-strip reflow, image/font growth, and polling renders, with the newest reply fully visible. Deliberately scrolling upward disengages it. Failed optimistic receipts never engage or advance follow-tail. | Implemented; desktop + mobile growth pass |
| MCV-13 | Sending through the normal composer while a question is open must decline the question and send the typed message as the follow-up. It must never select an option. | The browser submits the exact visible question nonce with the message. The server first accepts that nonce's native dismiss, then creates one idempotent when-available delivery. A changed/stale nonce, rejected dismiss, or uncertain dismiss sends no text and preserves the composer draft. Offline persistence retains the nonce and performs the same transaction after reconnection. Claude and Codex regressions prove no option/multi-answer action fires. | Implemented; desktop/mobile browser and Claude/Codex provider tests passed |

## Load-bearing decisions

### D1 — Multiline composer growth

Recommendation: one-row resting height exactly equal to Send; grow upward only after a real newline,
to a bounded four-line maximum. This preserves Return-as-newline without recreating the oversized
empty composer.

Status: settled as recommended.

### D2 — Keyboard resize anchor

Recommendation: if the reader is already at the bottom, remain pinned to the bottom. Otherwise,
preserve the same first visible message (chat) or text position (Markdown) while the visual viewport
opens and closes.

Status: settled as recommended.

### D3 — Failed optimistic receipt

Recommendation: keep **Restore** to put the exact text/images back in the composer, add **Dismiss** to
remove the failed receipt without restoring, and stop every failed receipt from forcing bottom
pinning. Either action resolves the durable receipt so it does not return after reload.

Status: settled as recommended.

## Implementation map

1. Reproduce P1-P3 from the current source, automated mobile viewport, and isolated staging; annotate
   measured gaps/control heights without waiting for screenshots.
2. Trace the canonical composer, photo picker/tap lifecycle, mobile visual-viewport variables, header renderers, file browser,
   status strip, received-file ordering, optimistic receipt lifecycle, and bottom-anchor logic.
3. Preserve `renderComposer` as the canonical shared component; correct its shared child order and
   compact geometry once, then remove any surface-specific overrides that make it render differently.
4. Build the paired upper action strips from the same grid contract:
   - Markdown: `file browser | Chat`
   - Chat: `status line | latest file`
5. Fix keyboard-safe viewport sizing and stable content anchoring for focus and blur.
6. Repair failed-receipt resolution and remove it from bottom-pinning authority. Replace the fragile
   12-pixel bottom test with an explicit follow-tail state that survives canonical reply and late
   layout growth but disengages when the reader scrolls upward.
7. Route composer sends during a visible question through exact-nonce dismiss-then-queue behavior;
   preserve the same intent in the offline queue and keep the draft if dismissal is not accepted.
8. Add deterministic mobile/desktop regressions for every MCV item, including repeated focus/blur,
   long files, long chats, trusted photo-picker taps, cancel/select image paths, image drafts,
   no-file sessions, and failed sends across reload.
9. Run backend, syntax, push, and doubled browser gates; record exact results here.
10. Restart isolated staging and complete the three-view real-iPhone test with the user.

## Verification ledger

- Requirements captured: complete.
- Reference screenshot inspection: waived by user; source/staging reproduction replaces it.
- Focused regressions: the complete MCV set passes on desktop and mobile. The highest-risk races were
  stress repeated: closed-chat repaint 10/10 mobile; resizable question drawer 20/20 desktop/mobile;
  notification action isolation 10/10 desktop/mobile; History routing/pagination/filtering 18/18.
  The compound question follow-up passed 6/6 desktop/mobile browser paths and 3/3 provider cases.
- Complete browser matrix: all 190 runnable functional cases passed; 15 live-environment cases were
  intentionally skipped. Stress runs exposed three additional responsiveness defects—New Session
  rebuilt the whole fleet, notification feedback rebuilt all 60 rows, and revisiting History silently
  paged another 100 rows. Those were fixed. The final mobile first-feedback inventory then passed
  3/3 consecutive full runs under the unchanged 100 ms budget; desktop passed its full inventory.
- Backend: 359/359 unit/integration tests passed.
- Push/PWA: 11/11 service-worker and Web Push tests passed.
- Syntax and patch hygiene: `node --check static/app.js` and `git diff --check` passed.
- Security heuristic review: the new notification-detail `innerHTML` assignment renders only the
  existing `notificationDetailHtml` builder, whose dynamic event fields are escaped; it introduces
  no raw server- or user-controlled HTML path.
- Real-iPhone staging gate: automated staging health check pending in this release step; screenshots
  were explicitly waived.
