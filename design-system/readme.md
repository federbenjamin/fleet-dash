# Fleet Dash "Console" design system

The design system distilled from the final Fleet Dash redesign (the "Console" direction). Fleet Dash is a self-hosted, single-operator mission-control PWA for a fleet of AI coding sessions (Claude Code + Codex CLI) on one Mac, used all day on desktop (~1440w) and phone (~390w). This system exists so new surfaces — screens, cards, notifications, settings sections — can be built to match the shipped redesign exactly.

**Sources** (ground truth, in this project):
- `Fleet Dash Redesign.dc.html` — master mockup canvas, every final screen/state
- `Fleet Dash Prototype.dc.html` / `Fleet Dash Prototype Light.dc.html` — interactive dark/light prototypes
- `fleet-dash/` mounted repo; brief at `fleet-dash/redesign-brief/BRIEF.md`
- Locked product decisions: `CLAUDE.md`; developer handoff: `design_handoff_fleet_dash/`

## CONTENT FUNDAMENTALS

Terse operator tone. This is a cockpit, not a marketing page — copy is information-dense, honest, and never cute.

- **Two registers.** Space Grotesk carries human language (titles, questions, chat, buttons: "Which deployment target should the release script promote first?", "Spawn session"). IBM Plex Mono carries machine language (status, paths, numbers, receipts).
- **Casing is semantic.** UPPERCASE mono for section headers and statuses: `NEEDS YOU · 2`, `WORKING · 2`, `DEPLOY ORDER — WAITING ON YOU`, `ANSWER IN PANE →`. lowercase mono for metadata and receipts: `fleet-dash · main · claude`, `delivered ✓`, `local daemon`, `markdown · 12 KB · 4m ago`. Sentence-case Space Grotesk for everything human ("Now", "Hold the release").
- **The middot `·` is the universal separator.** Never slashes or pipes: `3 options · recommended: Staging first`, `75% · resets 17:00 (~38m)`.
- **Delivery honesty.** State copy admits uncertainty rather than faking success: "sending — awaiting transcript confirmation", "queued · follows the declined question", "Queued offline". Never claim delivery you can't prove.
- **Numbers are facts, not decoration.** Every number is real state (ctx %, quiet time, cost, counts) in tabular mono. No emoji, ever. Second person only where needed ("waiting on you").

## VISUAL FOUNDATIONS

- **Color.** Warm near-black world: bg `#0E0D0B`, cards `#14120E`, hairlines `#262219`, warm off-white text `#EDE8DF`, three warm-gray dim steps (`#9A917F` → `#8F8878` → `#7E7666`). Five load-bearing accents: amber `#E5A83B` = needs you / nearing limits, green `#6FBF5F` = working/ok, red `#E5604F` = stalled/failed/limit, purple `#B99AE8` = model names, blue `#7FA9E0` = interactive/primary data. Accents appear as text, 6–7px dots, 1px borders, and ≤8% alpha surface tints — never as large fills. Light "paper" theme (`[data-theme="light"]`): `#F5F2EB` bg, white cards, `#E3DDD1` hairlines, darkened accents (amber `#B97F16`, green `#3E7D33`, red `#B23A2C`, blue `#2E66B0`, purple `#6D4FA8`).
- **Type.** Space Grotesk (400–700) + IBM Plex Mono (400–600) only. Page titles 600 22px; card titles 600 14–15px; body 13–13.5px; mono runs 9–12px with letter-spacing .06–.16em on labels. Tabular numerals everywhere.
- **Backgrounds.** Flat solids only. No images, textures, or gradients (sole exception: the light-mode needs-you card wash). Depth comes from hairlines and one-step surface shifts, not shadow.
- **Borders & shadows.** 1px hairlines everywhere; 2px only as active-tab underline. Effectively shadowless. Semantic borders tint by state: amber `#4A3A1C`, green `#2E4A26`, blue `#2A3547`, red `#5C2A22`.
- **Cards.** `#14120E` on `#0E0D0B`, 1px `#262219` border, 10px radius, `12–14px × 14–16px` padding. Desktop session cards carry a 118px meta rail on the right (`#12100C`, left hairline); mobile cards use inline status. Needs-you cards swap to amber border `#4A3A1C` + `#151109` surface.
- **Radii.** 10px cards → 8px options → 7px controls/inputs → 6px badges/chips → 5px access chips → 2px bars/grips. Never fully round except dots and avatars.
- **Buttons.** Primary = inverted fg-on-bg (`#EDE8DF` bg, `#0E0D0B` text), 600 11.5–12px Space Grotesk, 7px radius, `6–9px × 12–18px` padding. Secondary = transparent + `#3A311E` hairline. Danger = red text + red-tint border. The drawer's recommended option gets amber border + 7% amber tint.
- **States & motion.** Hover: border brightens one step / text lifts one dim step — no inversion, no transforms. Press: none (instant action). Animation: essentially none — the UI re-renders on a 2s poll, so layout stability beats motion. Spinners only on delivery receipts.
- **Layout.** Fixed 136px nav rail with usage-bar footer; left pane (queue/list) + persistent right session workspace; draggable splitter 650–1200px; Settings/Insights full-width; Settings has its own 210px section rail. Mobile = bottom tab bar, 44px targets, 16px inputs (no iOS zoom). Section headers = 7px status dot + UPPERCASE mono label + `· count`.
- **Transparency/blur.** None. Alpha only for the ≤8% accent tints.
- **Imagery.** None. File previews render on a light "paper" reading surface (`#F5F2EB`) inside the dark app.

## ICONOGRAPHY

No icon font and no SVG set — by design. Icons are **unicode glyphs rendered as text** in 1px-bordered squares (28–30px desktop, 34px composer ＋, 44px touch): `＋` add, `‹ ›` back/paging, `⤡ ⤢` expand/collapse, `✕` dismiss, `⋮` overflow (terminal lives here), `▾` disclosure, `→` follow-through, `✓` confirmation, `◆` question marker. Status is carried by 6–7px colored dots and 4-bar sparklines, not icons. **There is no logo**: the mark is the letter "F" in a 1px amber-bordered, 6px-radius tile beside "Fleet Dash" in 600 13px Space Grotesk. Do not draw a logo or import an icon set.

## Index

- `styles.css` → `tokens/` (`colors.css`, `typography.css`, `spacing.css`, `fonts.css`) — consumers link this one file
- `guidelines/` — foundation specimen cards (Colors, Type, Spacing, Status, Brand)
- `components/core/` — Button, IconButton, Chip, SectionHeader, StatusDot, UsageBar, Tabs, Receipt
- `components/session/` — SessionCard, AgentRow, QuestionCard, ApprovalCard
- `components/workspace/` — ChatMessage, Composer, QuestionDrawer
- `components/shell/` — NavRail, NotificationRow
- `ui_kits/fleet-dash/` — interactive Now screen (queue + session workspace)
- `SKILL.md` — agent-skill entry point

**Intentional additions:** none — the component inventory is exactly what the final mockups/prototypes define.
**Font note:** Space Grotesk and IBM Plex Mono load from Google Fonts (`tokens/fonts.css`); no binaries are vendored. Self-host them for the PWA build.
