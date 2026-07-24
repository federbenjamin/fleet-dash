# Fleet Dash "Console" design system — Claude Code handoff

Drop this folder into the Fleet Dash repo (suggested: `design-system/`) and point Claude Code at this file. It complements `design_handoff_fleet_dash/` (the screen-by-screen redesign spec); this package is the **reusable system** distilled from those mockups.

## What's here

- `readme.md` — **read first.** Content fundamentals (tone, casing, · separators), visual foundations (color/type/borders/motion), iconography rules.
- `styles.css` → `tokens/` — CSS custom properties: colors (dark default + `[data-theme="light"]` paper theme), type scale, spacing/radii/structural widths, font imports. **This is the single source of truth for values** — reference the vars, don't hardcode hex.
- `components/` — 17 reference components in 4 groups:
  - `core/` — Button, IconButton, Chip, SectionHeader, StatusDot, UsageBar, Tabs, Receipt
  - `session/` — SessionCard, AgentRow, QuestionCard, ApprovalCard
  - `workspace/` — ChatMessage, Composer, QuestionDrawer
  - `shell/` — NavRail, NotificationRow
  Each has three files: `Name.jsx` (reference implementation, React + inline styles), `Name.d.ts` (props contract), `Name.prompt.md` (usage rules + example). The `.jsx` files are **specs, not a library** — Fleet Dash is a vanilla-JS PWA, so port the markup/styles/behavior into its rendering approach; the props contracts define each component's API surface.
- `guidelines/` + `design-system.html` — visual specimen gallery; open `design-system.html` in a browser to see everything rendered.
- `ui_kits/fleet-dash/index.html` — interactive Now screen composed from the components; the reference for how they fit together (shell → queue → workspace → drawer → composer).
- `SKILL.md` — the non-negotiables in one paragraph; usable as a Claude skill.
- `ds-preview.js` — browser loader for the demo pages only; not part of the system.

## Rules that override everything

1. Inline the token values via CSS vars; never invent colors — the five accents are semantic (amber=needs-you, green=working, red=problems, purple=models, blue=interactive).
2. Usage thresholds everywhere: amber ≥70%, red ≥90%.
3. No icon set, no logo, no emoji, no shadows, no gradients — unicode glyphs in bordered squares, 1px hairlines, flat surfaces.
4. Locked behaviors live in each `*.prompt.md` (e.g. QuestionCard never shows selectors; NotificationRow actions attach as a sub-card; terminal only behind ⋮).
5. Layout stability beats motion: the UI re-renders on a 2s poll — no transforms, no transitions that reflow.

## Fonts

Space Grotesk (400–700) + IBM Plex Mono (400–600). `tokens/fonts.css` pulls from Google Fonts — self-host both for the PWA build and replace that import.

## Suggested kickoff prompt

> Read design-system/HANDOFF.md, then readme.md and SKILL.md. When building any Fleet Dash surface, use the tokens in styles.css and match the reference components in components/ (jsx = visual/behavior spec, d.ts = props API, prompt.md = usage rules). Check your work against design-system.html and ui_kits/fleet-dash/index.html in a browser.
