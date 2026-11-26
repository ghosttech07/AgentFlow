---
name: AgentFlow
description: A reference desk for trustworthy AI work; every answer and workflow is a catalogued index card with its evidence filed beside it.
colors:
  ground: "#e9edf1"
  surface: "#f8f9fb"
  card: "#fbf7ea"
  card-line: "#e4d8bb"
  line: "#d3d9e0"
  ink: "#1d1f24"
  muted: "#555c67"
  oak: "#2b211b"
  oak-2: "#44342a"
  on-oak: "#f3ede3"
  brass: "#d2a457"
  brass-ink: "#7d5a1c"
  rule: "#b3261e"
  link: "#9a2a1f"
  ok: "#1d6b3a"
  warn: "#8a5a00"
  bad: "#b42318"
  info: "#1f4e8c"
  idle: "#5c6370"
  ground-dark: "#121417"
  surface-dark: "#191c21"
  card-dark: "#211d16"
  card-line-dark: "#3d3428"
  line-dark: "#2d323a"
  ink-dark: "#e9e6e0"
  muted-dark: "#b0aca3"
  oak-dark: "#33271f"
  oak-2-dark: "#4a3a2e"
  on-oak-dark: "#f3ebdf"
  brass-dark: "#dcb064"
  brass-hover-dark: "#e8c27f"
  on-brass-dark: "#1a140f"
  rule-dark: "#e05a4e"
  link-dark: "#f09a8f"
  ok-dark: "#62c48f"
  warn-dark: "#e3ab4c"
  bad-dark: "#f2806f"
  info-dark: "#93b6ee"
  idle-dark: "#9ba2ae"
typography:
  wordmark:
    fontFamily: "Libre Franklin, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 750
    letterSpacing: "-0.01em"
  headline:
    fontFamily: "Libre Franklin, system-ui, sans-serif"
    fontSize: "19px"
    fontWeight: 700
    letterSpacing: "-0.01em"
  drawer-label:
    fontFamily: "Libre Franklin, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.01em"
  reading:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "15.5px"
    lineHeight: 1.65
  body:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  button:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "13px"
    fontWeight: 600
    lineHeight: 1
  call-number:
    fontFamily: "Courier Prime, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1
  data:
    fontFamily: "Courier Prime, ui-monospace, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    fontFeature: "tnum"
  data-figure:
    fontFamily: "Courier Prime, ui-monospace, monospace"
    fontSize: "14px"
    fontWeight: 700
    lineHeight: 1
    fontFeature: "tnum"
  pill:
    fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1
rounded:
  seg: "2px"
  track: "3px"
  pre: "4px"
  r: "6px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "6px"
  sm: "8px"
  md: "10px"
  ml: "12px"
  lg: "16px"
  xl: "18px"
  margin-column: "28px"
components:
  button-primary:
    backgroundColor: "{colors.oak}"
    textColor: "{colors.on-oak}"
    typography: "{typography.button}"
    rounded: "{rounded.r}"
    padding: "8px 12px"
  button-primary-hover:
    backgroundColor: "{colors.oak-2}"
  button-primary-dark:
    backgroundColor: "{colors.brass-dark}"
    textColor: "{colors.on-brass-dark}"
  button-primary-dark-hover:
    backgroundColor: "{colors.brass-hover-dark}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.button}"
    rounded: "{rounded.r}"
    padding: "8px 12px"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.bad}"
    rounded: "{rounded.r}"
    padding: "8px 12px"
  chip-follow-up:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "6px 10px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.r}"
    padding: "7px 9px"
  input-ask:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r}"
    padding: "11px 13px"
  index-card:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r}"
    padding: "16px 18px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r}"
    padding: "16px"
  oak-bar:
    backgroundColor: "{colors.oak}"
    textColor: "{colors.on-oak}"
    padding: "10px 20px"
  status-pill:
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 7px"
  call-number:
    textColor: "{colors.brass-ink}"
    typography: "{typography.call-number}"
---

# Design System: AgentFlow

## Overview

**Creative North Star: "The Reference Desk"**

AgentFlow is a library reference desk for AI work. Every answer and every workflow is a catalogued index card: manila stock, one catalog-red rule under its heading, a typewritten call number in the corner. You trust a card because its evidence is filed with it; sources sit as numbered "see also" tracings in a margin column beside the text they support, never in a footnote pile below it.

The shell is the desk itself: a dark oak top bar with brass labels, a cool, quiet reading-room ground, and plain white-grey panels that work like labelled drawers. Index cards are the only warm, lifted things on the page, so the eye goes to the work. The system is dense and operational (Operate mode): a fast daily tool first, legible to a demo audience second, never a grid of grey stat tiles or floating marketing cards.

Status colours (ok green, warn amber, bad red, info blue) are standard operational signals laid on top of the world, not part of its palette. Motion is nearly absent: one moment, an answer "filing in". Live refresh is invisible: only changed regions are rewritten, and focus and scroll survive.

**Key Characteristics:**
- Oak top bar, cool ground, white-grey drawer panels, manila index cards.
- One catalog-red rule under every index card heading; it follows the heading when it wraps.
- Courier Prime only for data: call numbers, ids, costs, tokens, provider:model, offsets.
- Libre Franklin bold headings like printed drawer labels; system UI for everything read or typed.
- Sources as "see also" tracings in a margin column beside a 72ch answer.
- One motion moment; light and dark both first-class.

## Colors

A restrained, material palette: wood, brass, manila and a single catalog red over a cool grey reading room, with standard status colours on top. Every light token has a dark counterpart (`-dark` keys) applied through `prefers-color-scheme`.

### Primary
- **Desk Oak** (oak): the top bar, the toast, and the primary action in light mode. Its hover is **Lighter Oak** (oak-2), which also strokes the bar's settings disclosure.
- **Catalog Red** (rule): the single ruled line under an index card heading, the left rule of the tracings column, tracing numbers, the selected catalog row's underline, and the text caret. **Rubric Red** (link) is its slightly deeper sibling for links and in-text citations.

### Secondary
- **Brass** (brass): labels and figures on the oak bar only (health counts), and the selection highlight (38% mix). In dark mode brass becomes the primary action fill and the meter fill, with **Oak Ink** (on-brass-dark) as its text, because oak is too quiet on dark cards.
- **Brass Ink** (brass-ink): call numbers and the focus outline on light stock. Brass itself is too pale on manila; this is its legible ink.

### Neutral
- **Reading-Room Ground** (ground): the page background, cool and recessive.
- **Drawer Grey** (surface): panels, inputs, secondary buttons, the settings popover.
- **Manila** (card) with **Manila Edge** (card-line): index card stock and its border; card-line also draws the dashed rule above the budget strip.
- **Rule Grey** (line): panel borders, table rows, drawer-label leaders, input strokes.
- **Ink** (ink) and **Muted Ink** (muted): text; muted carries meta, sub-labels, table headers, placeholders.
- **On Oak** (on-oak): text on the bar and toast.

### State (on top of the world)
- **Ok** (ok): completed / succeeded. **Warn** (warn): awaiting approval, paused, retrying, interrupted, budget-paused, fallbacks, hot meters. **Bad** (bad): failed, worker lost, errors, danger buttons, full meters. **Info** (info): in-motion states only (planning, ready, running). **Idle** (idle): every other status.
- State colour is always applied as one hue: text in the hue, a 13% tint background, a 30% tint border.

### Named Rules
**The One Red Line Rule.** An index card has exactly one catalog-red rule, under its heading. Red is a filing mark and a link colour, never a fill or a panel border.

**The Brass Belongs To The Desk Rule.** Raw brass appears only on oak (bar labels) or as the dark-mode primary/meter fill. On light card stock, call numbers use brass-ink.

**The States Sit On Top Rule.** ok/warn/bad/info are operational signals, not decoration. Info blue marks work in motion and nothing else.

## Typography

**Display Font:** Libre Franklin (variable 300-800, self-hosted; falls back to system-ui)
**Body Font:** system UI stack (system-ui, -apple-system, Segoe UI, Roboto, sans-serif)
**Label/Mono Font:** Courier Prime (400 and 700, self-hosted; falls back to ui-monospace)

**Character:** Franklin reads like the printed labels on card-catalog drawers: bold, compact, slightly tight. Courier Prime is the library typewriter that stamped the call numbers, so it is reserved for data. The system face carries everything a person reads at length or types.

### Hierarchy
- **Wordmark** (750, 18px): "AgentFlow" on the oak bar only.
- **Headline** (700, 19px): index card titles; balanced wrapping, breaks anywhere for long goals.
- **Drawer label** (700, 12px, 1.2): panel headings, "See also", table headers (in muted ink), provider names at 13.5px.
- **Reading** (400, 15.5px, 1.65): the Quick Ask answer, capped at 72ch.
- **Body** (400, 14px, 1.5): everything else; meta and stats step down to 12-12.5px in muted ink.
- **Call number** (Courier 700, 12px, brass-ink): `AF-XXXXXX` on every workflow card, approval card and catalog row.
- **Data** (Courier 400, tabular figures): ids, provider:model, token and request counts, timeline offsets, JSON. Budget figures use Courier 700 at 14px.

### Named Rules
**The Typewriter Is For Data Rule.** Courier Prime appears only on machine facts: call numbers, ids, costs, tokens, provider:model, offsets, JSON. Never on headings, prose or buttons.

**The Drawer Label Rule.** A panel heading is bold Franklin at 12px followed by a 1px ruled leader that runs to the panel's edge. No uppercase tracking, no small caps line above a heading.

## Layout

A sticky oak bar (10px 20px padding, wraps on narrow screens), then a centered desk up to 1440px wide with 18px gutters and 18px between stacked blocks. Quick Ask spans the full width directly under the bar. Below it, two work columns: a 280-340px left stack (file a goal, providers drawer, catalog) and a fluid right column holding the selected workflow card, its approval cards, attempt lanes, tasks, timeline and result. Under 900px the columns stack and the selected workflow moves above the catalog.

The answer is a two-column grid: text up to 72ch, then a 200-300px tracings margin column, 28px apart; under 760px the tracings drop below the text. The budget strip is four equal cells (two under 760px). Attempt lanes give the task key 200px (124px narrow) and the track the rest. The catalog and timeline scroll inside 380px boxes; wide tables scroll horizontally.

Spacing rhythm runs 4 / 6 / 8 / 10 / 12 / 16 / 18px: 16px panel padding, 16px x 18px card padding, 10px under a card heading before its rule, 14px from the rule to the body.

## Elevation & Depth

Mostly flat with one lifted material. Panels sit flat on the ground with a 1px border; index cards (and the two floating surfaces, the settings popover and the toast) carry a soft two-layer ambient shadow, so warmth plus lift marks what is filed work. The oak bar separates with a 1px dark hairline, not a blur.

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 1px 2px rgba(29,31,36,.06), 0 6px 18px -10px rgba(29,31,36,.22)`; dark: `0 1px 2px rgba(0,0,0,.3), 0 8px 22px -12px rgba(0,0,0,.6)`): index cards, settings popover, toast.
- **Bar hairline** (`box-shadow: 0 1px 0 rgba(0,0,0,.25)`): the sticky oak bar only.
- **Selected row underline** (`box-shadow: inset 0 -2px 0` catalog red): the current catalog row, which also takes manila stock.

### Named Rules
**The Only Cards Lift Rule.** Panels are flat drawers; only index cards and transient overlays get the card-lift shadow. No hard offset shadows.

## Shapes

Gently softened, paper-like rectangles: one radius (6px) for panels, cards, inputs, buttons, banners and the toast. Smaller corners for small parts (4px code blocks, 3px tracks and the focus ring, 2px lane segments and legend keys). Fully round only for status pills, follow-up chips, meters and status dots. Borders are always 1px; the only dashed line is the budget strip's top rule on card stock. Line icons are inline SVG at 14px with a 1.8 stroke and round caps.

## Components

### Buttons
Plain and firm; the primary is a block of desk material.
- **Shape:** softened rectangle (6px), 1px border, 600 13px system UI, 8px 12px padding.
- **Primary:** oak fill with on-oak text in light; brass fill with oak ink in dark. Hover goes one step lighter (oak-2 / brass hover).
- **Secondary:** drawer-grey fill, ink text, rule-grey border; hover darkens the border toward ink.
- **Danger:** secondary shape with bad-red text (reject and stop).
- **Disabled:** 45% opacity, not-allowed cursor. Transitions are 150ms on colour only.
- **Focus (all interactive):** 2px brass-ink outline, 2px offset.

### Chips
- **Follow-up questions:** pill-shaped secondary buttons (500 12.5px, 6px 10px) under an answer; clicking one asks it.
- **Status pills:** 600 11px, 4px 7px, one state hue as text with a 13% tint fill and 30% tint border.

### Cards / Containers
- **Index card (signature):** manila fill, manila-edge border, 6px corners, card-lift shadow, 16px 18px padding. Its heading row (title, then tags: status pill and call number) has 10px below it and one 1px catalog-red bottom border that wraps with the heading. Used for Quick Ask, the selected workflow, and each approval.
- **Approval card:** an index card of its own: lock icon + task name + muted "needs your decision" in the heading, expiry and call number as tags, then why-sensitive, the task, a disclosure of the exact input and upstream previews (Courier 12px on a 4% ink tint), comment and JSON-override fields, and Approve / Reject and skip / Reject and stop.
- **Drawer panel:** drawer-grey fill, rule-grey border, 6px corners, 16px padding, no shadow, drawer-label heading.
- **Budget banner:** warn border (45%) over a 10% warn tint of manila, strong warn headline, inline limit form with Save limits and a primary Resume.

### Inputs / Fields
- **Style:** drawer-grey fill, 1px rule-grey stroke, 6px corners, 7px 9px padding, body type; placeholders in muted ink at full opacity. Field labels sit above in 12px muted.
- **Ask field:** 16px type, 11px 13px padding, Ask button to its right.
- **Focus:** the global brass-ink outline. The caret is catalog red.
- **Error:** messages in bad red; no red field strokes.

### Navigation
- **Oak bar:** wordmark, engine and provider health (status dots plus brass counts, 12.5px on-oak at 82%), then an "Operator" disclosure (gear icon) opening the settings popover for actor name and API key. Health dots on oak use the dark-scheme state values in both modes, since the bar is dark in both.
- **Catalog:** rows with a two-line clamped title (600), then call number, short date and status pill. Hover: 4% ink tint. Selected: manila stock, manila-edge border, catalog-red underline. Rows are keyboard-selectable (Enter / Space); `#<workflow_id>` deep-links.

### Tracings ("See also")
The margin column beside an answer: a 1px catalog-red left rule, 16px inset, a drawer-label "See also" heading, then numbered entries (Courier bold red numbers, a 2.2em gutter, linked title, muted domain). Sources read but not cited are muted and say so. In-text citations are Courier bold 11px superscript `[n]` links.

### Attempt Lanes and Meters
- **Lanes:** task key and model per row, a 18px track on a 6% ink tint (3px corners), attempt segments 12px tall in their outcome's state hue (2px corners).
- **Meters:** 6px round bars on a 10% ink tint; fill is oak in light and brass in dark, warn at 80%, bad at 100%.

### Motion and Live Refresh
- **Filing in:** a new answer rises 6px and fades in over 320ms on `cubic-bezier(.16, 1, .3, 1)` (expo ease-out). It is the only animation; `prefers-reduced-motion` removes it and all transitions.
- **Region swaps:** each region is rewritten only when its markup changed; a region holding a focused text field is not touched; focus returns to the same control and inner scroll positions are kept.

## Do's and Don'ts

### Do:
- **Do** put filed work (answers, workflows, approvals) on index cards: manila, one catalog-red rule under the heading, a brass-ink Courier call number.
- **Do** keep Courier Prime to data: call numbers, ids, costs, tokens, provider:model, offsets, JSON.
- **Do** head every drawer panel with a bold 12px Franklin label and its ruled leader.
- **Do** file sources as numbered "see also" tracings in a margin column beside a 72ch text, and cite them inline as `[n]`.
- **Do** use oak for the primary action in light mode and brass with oak ink in dark mode; links are catalog red (rubric red in prose).
- **Do** apply state hues as text + 13% tint + 30% border, and reserve info blue for planning, ready and running.
- **Do** update live data by swapping only changed regions, keeping focus and scroll.
- **Do** keep motion to the one filing-in moment and honour `prefers-reduced-motion`.

### Don't:
- **Don't** build grey stat tiles or floating marketing cards; numbers live in the budget strip, stats line and lanes.
- **Don't** draw more than one red rule on a card, or use catalog red as a fill or panel border.
- **Don't** set raw brass on light card stock; use brass-ink.
- **Don't** give flat drawer panels the card-lift shadow, or use hard offset shadows.
- **Don't** put Courier on headings, prose or buttons, or set headings in the system face.
- **Don't** add uppercase eyebrow or kicker lines above headings.
- **Don't** use text glyphs or emoji as icons; icons are 14px inline SVG line icons.
