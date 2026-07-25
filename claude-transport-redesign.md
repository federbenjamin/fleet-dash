# Can the Claude path be redesigned to resemble the Codex path?

Short answer: **not the transport — that door is closed.** But "resembles Codex" decomposes into four
separate properties, and three of them are reachable without a transport. Only one of them is where
the felt latency actually lives, and it needs nothing from Anthropic.

---

## The premise that doesn't hold

Codex's structural control comes from `codex app-server` — a JSON-RPC server Fleet starts and owns,
reachable over a Unix socket. **Claude Code has no equivalent.** Verified against current docs:

- No app-server, control socket, daemon, `--listen` flag, or RPC endpoint for an already-running
  interactive `claude` TUI session.
- `claude attach <job>` / `claude stop <job>` are fixed CLI commands wrapping an internal supervisor
  protocol. Not connectable by an external process, not documented, not enumerable. (Fleet already
  uses these correctly for `kind:bg` sessions — invariant 25 — and that is the ceiling.)
- No IDE-extension protocol, no `/mcp` control endpoint, no experimental socket.
- `--permission-prompt-tool` exists but applies to `--print`/SDK mode, not to a running TUI.
- `--input-format stream-json` is for feeding follow-up *messages* into a headless run. It is not a
  bidirectional control channel and has no interrupt/permission/model control protocol.

So there is no version of this where Fleet opens a socket to the `claude` process already running in
your iTerm tab and calls `turn/steer` on it. Keystroke injection is the only external control path
for a running interactive session, and that is a documented architectural difference, not a gap in
Fleet.

## What "resembles Codex" actually means — four properties

| # | Property Codex has | Reachable for Claude? | Where the pain is |
|---|---|---|---|
| 1 | Actions return immediately; delivery is reconciled asynchronously | **Yes, fully** — pure Fleet-side | **This is ~all of the felt latency** |
| 2 | One provider-owned identity per pending request | **Yes, fully** — pure Fleet-side | The reappearing prompt |
| 3 | Answers are structured data, not synthesized keys | **Partially**, via a hook trick with a real cost | Fragility, 0.4 s/key |
| 4 | Fleet owns the process lifecycle | **Only by giving up the iTerm TUI** | Product change, not a refactor |

Properties 1 and 2 are the entire content of findings 1, 2 and 4 in the latency audit. Neither
requires anything from Claude Code. They are the redesign worth doing.

---

## Property 1 — async action API with a durable receipt

Make `/api/act` behave like a Codex RPC from the browser's point of view: validate, enqueue, return
in ~10 ms with a receipt id. A per-session worker takes `scan_lock`, drives the applet, and updates
the receipt. The receipt rides `/api/fleet`; the client's spinner is driven by receipt state, not by
an open fetch.

The keystrokes still take 0.4 s each. Nobody waits on them.

This is strictly better than today's error semantics, not just faster: right now a dropped connection
mid-fetch produces "delivery uncertain" with nothing durable behind it. A receipt survives reload,
backgrounding, daemon restart and network loss — which is exactly what invariant 66 wants and cannot
currently guarantee.

**Cost:** ~20 client `act()` call sites read `d.ok` synchronously today. Needs a receipt store and
reconciliation. Touches invariants 34, 61, 66.

## Property 2 — one server-owned request identity

Codex has exactly one identity per pending request, owned by the protocol. Claude has two namespaces
for the same prompt — `hook-<epoch_ms>` from the capture hook, `toolu_…` from the transcript fallback
— and the client suppresses on whichever it last saw. That is the reappearing prompt.

Fix: derive a stable `request_id` both sources map to, and add a server-side answered fence so an
accepted answer suppresses the prompt for *every* client until it is proven resolved. This also
closes a hazard that exists today and has nothing to do with latency: two devices can both answer the
same prompt.

**Cost:** contained, server-side. Full detail in `latency-audit.md` finding 4 / options 4A–4C.

## Property 3 — structured answers instead of synthesized keys

There is one real mechanism here, and it has a genuine trade-off.

A `PreToolUse[AskUserQuestion]` hook can return `permissionDecision: "deny"` with a
`permissionDecisionReason`, and that reason becomes what the model sees in place of the tool result.
Hooks may block for up to 600 s. So Fleet's capture hook could, instead of returning instantly:

1. register the pending question with the daemon,
2. **wait** for an answer from the dashboard,
3. return the chosen answers as the decision reason — zero keystrokes, one identity, instant delivery,
4. and on timeout, return `allow` so the normal TUI prompt appears and keystroke injection stays as
   the fallback.

This removes the worst path entirely: the 0.4 s/key sequencing, the DOWN-walks, the phantom-Enter
class of bugs, the whole of invariant 4.

**The cost is not small.** While the hook waits, the TUI shows nothing — the session is frozen mid-
tool with no visible question. Someone sitting at the Mac cannot answer locally during that window,
because the prompt hasn't rendered yet. So the wait length is a direct trade between "phone answers
land instantly" and "the terminal stays usable". A 3-second window keeps the terminal usable but the
phone almost never wins the race; a 5-minute window makes Fleet the primary interface and the
terminal a fallback.

Secondary cost: the model receives the answer as a denial reason rather than a native
`AskUserQuestion` result, so the transcript shape changes — the `qa` event Fleet renders
(invariant 11) would need to be derived differently, and Claude's own view of the exchange is prose
rather than structured.

This is a per-session product decision, not a global one.

## Property 4 — Fleet owns the process

True Codex parity means Fleet spawns and owns the agent process. For Claude that means the Agent SDK:
`canUseTool` callbacks, hooks, structured streaming — full programmatic control.

But an SDK session runs the agent loop **in Fleet's process**. There is no TUI. A human cannot watch
or interact with it in an iTerm tab. That inverts the product: today Fleet is a remote control for
sessions you drive locally; this makes Fleet the place sessions live, with no local terminal at all.

That may be a direction worth taking deliberately — but it is a different product, not a redesign of
this one, and it would be additive (a new session kind) rather than a migration.

---

## Recommendation

Do properties 1 and 2. They are pure Fleet-side work, they cover findings 1, 2 and 4 of the latency
audit, and they deliver essentially all of the responsiveness gain — because the problem was never
that keystrokes are slow, it was that the UI waits on them.

Treat property 3 as an opt-in per-session mode once 1 and 2 land, if the frozen-terminal trade is
acceptable to you.

Treat property 4 as a separate product question, not part of this work.
