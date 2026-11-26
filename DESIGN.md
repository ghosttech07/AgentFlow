# AgentFlow design system — "Answer engine" (2026-11-26)

Replaces the earlier "reference desk" world (oak bar, manila cards), which the owner rejected as cluttered.
The owner chose a clean answer engine in the family of Perplexity and ChatGPT: one question box,
answers with their sources, and a history of everything asked. Technical status stays off the screen.

## Principles
1. **One place to ask.** A single composer with two modes: Quick answer (cited answer in seconds) and
   Deep research (the agent team plans, researches and writes a report). The mic is present; voice is not
   built yet and says so when pressed.
2. **Nothing technical by default.** No engine status, providers, model names or call numbers on screen.
   Cost, AI calls, limits and the activity log live in a closed "Details" section on a research view.
3. **History is the navigation.** The left sidebar lists questions and research, newest first, each with a
   plain status ("Done", "Working", "Needs your approval", "Failed").
4. **Humans decide in plain words.** Approval cards say what will happen and what will be sent, with
   "Approve", "Don't do it" and "Stop" buttons. Warnings (untrusted web content, suspicious text) are shown
   in a calm amber note.

## Tokens (app/static/dashboard.html `:root`)
| Token | Light | Dark | Use |
|---|---|---|---|
| --bg | #f7f7f5 | #141517 | page ground |
| --side | #efefec | #1b1c1f | history sidebar |
| --surface | #ffffff | #1f2023 | composer, cards, report |
| --ink / --ink-2 / --ink-3 | #1b1c1e / #55585e / #6f737a | #ececea / #b3b4b8 / #9a9ba1 | text levels |
| --line / --line-2 | #e2e2de / #d3d3ce | #2d2e33 / #3a3b41 | borders |
| --accent | #0d6b5f | #4fc2ad | send button, selection, progress, links, citation chips |
| --ok / --warn / --bad | green / amber / red | | status dots and notes only |

Light or dark follows `prefers-color-scheme`. One accent (deep teal); nothing else is coloured except status.

## Type
- Wordmark and view headings: Libre Franklin 700-800 (self-hosted variable font), tight tracking
  (-0.02 to -0.035em). Home wordmark 56px (42px under 860px); sidebar wordmark 28px.
- Everything else: the system UI sans, 15px base; answers 16.5px / 1.7 at max 70ch.
- Monospace only for raw JSON in approval cards and details.

## Layout
- Desktop: 280px history sidebar + main column (max 780px, centred). Home centres the wordmark and the
  composer vertically; on answer and research views the composer docks at the bottom (sticky).
- Under 860px: the sidebar becomes an off-canvas drawer opened from a top bar (menu + wordmark).

## Components
- **Composer**: 18px radius, soft shadow, auto-growing textarea (Enter sends, Shift+Enter new line),
  mode toggle (pill segmented control), mic icon button, round accent send button.
- **Sources**: a grid of small source cards (title, number, domain) above the answer; citations in text are
  small accent chips linking to the page; uncited pages are dimmed.
- **Research view**: title (the goal), status pill, plain-language actions (Pause, Resume, Try again,
  Cancel), approval cards, the report (writer output rendered as safe Markdown), progress bar, steps list
  (step name, agent, status), and the closed Details section.
- **Toast**: bottom-centre, dark, 4.2s; red for errors.
- Icons: one inline SVG set, 1.8 stroke, round caps.

## Motion
150-250ms ease-out on state changes only (toast, drawer, progress). Running items pulse their dot;
`prefers-reduced-motion` turns both off. Skeleton bars while an answer or research loads.

## Safety
All server strings are HTML-escaped before rendering; the Markdown renderer escapes first and only rebuilds
headings, lists, bold and paragraphs.
