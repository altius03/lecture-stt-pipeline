# Design — Lecture STT

The current web panel follows the operator flow: status → finished job detail,
completed quality checks, logs, and settings. Use this file when changing its UI.
`frontend/web-panel/src/panel-redesign.css` owns the current surface palette and
layout; `frontend/web-panel/tokens.css` retains shared typography, spacing, and
compatibility roles for older workbenches.

## Audience, use, and tone

- Audience: one operator responsible for the local lecture transcription system.
- Primary use: read the whole operating state, identify pending work, inspect
  evidence, and run an already-guarded action.
- Tone: technical, calm, and exact. The interface should read like an instrument
  panel, not a marketing page.

## Genre

`modern-minimal`

## Macrostructure family

- Marketing pages: not in scope.
- App pages: `Workbench`. The stable frame is a functional command rail, a
  compact runtime strip, and a variable-density work canvas. Screens may use a
  queue/detail split, ledger, table, or focused action panel according to their
  data.
- Content pages: not in scope.

The Hallmark N3 rail is not used: its rotated wordmark and section dots are for
an ordinal long page. Lecture STT instead uses a product-specific command rail
whose labels, descriptions, badges, and active state all carry operational
meaning.

## Theme

Neutral zinc, with colour used only for meaningful status and keyboard focus.

- Background/surface: `#fff`; muted surface: `#fafafa`; text: `#18181b`.
- Rule: `#e4e4e7`; main action: `#18181b`; focus: `#2563eb`.
- Dark mode follows the same surface hierarchy with `#111113` background.

Green, amber, and red are reserved for actual status, warnings, and errors.
Status is text plus a small dot or pictogram, never a repeated coloured pill.
Do not use generated photos or decorative imagery in the operator workflow.

## Typography

- Display: `Pretendard Variable`, weight 700, normal.
- Body: `Pretendard Variable`, weight 500.
- Telemetry/code: platform monospace stack, weight 600.
- Display tracking: `-0.025em`.
- Type-scale anchor: `--text-display = clamp(2.25rem, 4vw + 0.5rem, 4rem)`.

The primary Korean face is deliberately shared by display and body. Monospace is
a functional register for timestamps, storage keys, counts, and machine state;
it is not a decorative label applied to every heading. Numeric surfaces use
tabular figures.

## Spacing

The shared scale lives in `frontend/web-panel/tokens.css` and follows a four
pixel rhythm. The current panel fixes its measured layout values in
`panel-redesign.css` and reuses the shared tokens where they fit.

## Layout

- Desktop: command rail and main workbench form an intentionally unequal grid.
- The top strip shows runtime state; the rail owns product identity and routes.
- Home uses one aligned job table with four equal stage columns. Detail uses
  one transcript surface and a smaller metadata surface. Review uses a
  queue/detail split only when there is something to inspect.
- At narrow widths the command rail becomes a compact horizontal navigation
  surface and the workbench becomes one column.
- The document never scrolls horizontally. Dense tables may own a bounded
  internal scroll region when a mobile card representation would obscure
  evidence.

## Motion

- Easings: `--ease-out`, `--ease-in`, and `--ease-in-out` from `tokens.css`.
- State changes: specific colour, opacity, or transform transitions only.
- No global reveal, parallax, animated background, hover-scale grid, or
  `transition-all`.
- Reduced motion: spatial feedback is removed or becomes an opacity change of at
  most 150 ms.

## Microinteractions stance

- Successful guarded actions are silent when the resulting state is already
  visible.
- Errors remain explicit and preserve the existing retry path.
- Focus rings appear immediately and are never animated.
- Hover is optional enhancement under a hover-capable media query; every action
  remains available to keyboard and touch users.
- Buttons keep one-line labels and a minimum 44 px touch target.
- No new optimistic update or Undo semantics are introduced without matching
  backend support.

## CTA voice

- Primary: solid ink, 6 px radius, explicit verb naming the existing guarded
  action.
- Secondary: paper surface with a visible rule and ink text.
- Destructive: semantic error treatment only where the underlying operation is
  actually destructive.

## Per-page allowances

- App pages MUST NOT use enrichment. Function and real data carry the page.
- Home may foreground verified counts and operating state, but must not invent a
  metric.
- Processing may use the graphite log surface once; other screens remain on the
  paper system.
- Review, timetable, and title workflows may use queue/detail splits when their
  existing data supports them.

## What pages MUST share

- Lecture STT identity, functional command rail, and runtime strip.
- Neutral light/dark surface hierarchy and small semantic status marks.
- Pretendard and telemetry-mono roles.
- Button height, radius, focus treatment, and guarded-action voice.
- Left-aligned screen heading rhythm with no decorative ordinal eyebrow.
- Loading, empty, error, disabled, and selected state vocabulary.

## What pages MAY differ on

- Workbench density and the split between overview, queue, detail, and evidence.
- Whether the screen uses a ledger, table, definition grid, or log surface.
- The amount of contextual copy needed to explain a fail-closed boundary.

## Protected behavior

Visual work preserves write guards, confirmation defaults, immutable storage
keys, file paths, and operating data. The 2026-09-28 user-flow redesign adds
the job detail, quality decision, and transcript preview contracts documented
in `docs/ARCHITECTURE.md`. It adds no upload or automatic materialization.

## Exports

The excerpts below are historical compatibility mappings for the original
Cobalt system. Current surface values are in `panel-redesign.css`.

### tokens.css

```css
:root {
  --color-paper: oklch(98.3% 0.006 252);
  --color-paper-2: oklch(96.4% 0.009 252);
  --color-paper-3: oklch(93.8% 0.012 252);
  --color-ink: oklch(22% 0.022 258);
  --color-ink-2: oklch(31% 0.021 258);
  --color-rule: oklch(88% 0.014 254);
  --color-rule-2: oklch(72% 0.018 255);
  --color-muted: oklch(46% 0.018 257);
  --color-neutral: oklch(36% 0.02 258);
  --color-accent: oklch(50% 0.2 256);
  --color-accent-ink: oklch(98.5% 0.006 252);
  --color-focus: oklch(20% 0.025 258);

  --font-display: "Pretendard Variable", "Pretendard", "Noto Sans KR", ui-sans-serif, sans-serif;
  --font-body: "Pretendard Variable", "Pretendard", "Noto Sans KR", ui-sans-serif, sans-serif;
  --font-mono: "SFMono-Regular", "Cascadia Mono", "Roboto Mono", ui-monospace, monospace;

  --space-3xs: 0.25rem;
  --space-2xs: 0.5rem;
  --space-xs: 0.75rem;
  --space-sm: 1rem;
  --space-md: 1.5rem;
  --space-lg: 2rem;
  --space-xl: 3rem;
  --space-2xl: 4.5rem;
  --space-3xl: 7rem;

  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-base: 1rem;
  --text-md: 1.125rem;
  --text-lg: 1.375rem;
  --text-xl: 1.75rem;
  --text-2xl: 2.25rem;
  --text-display: clamp(2.25rem, 4vw + 0.5rem, 4rem);

  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in: cubic-bezier(0.7, 0, 0.84, 0);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --dur-micro: 120ms;
  --dur-short: 220ms;
  --dur-long: 420ms;
  --rule-hair: 1px;
  --rule-fine: 2px;
  --radius-card: 10px;
  --radius-input: 6px;
  --radius-pill: 999px;
}
```

### Tailwind v4 `@theme`

```css
@theme {
  --color-paper: oklch(98.3% 0.006 252);
  --color-paper-2: oklch(96.4% 0.009 252);
  --color-paper-3: oklch(93.8% 0.012 252);
  --color-ink: oklch(22% 0.022 258);
  --color-ink-2: oklch(31% 0.021 258);
  --color-rule: oklch(88% 0.014 254);
  --color-accent: oklch(50% 0.2 256);
  --color-focus: oklch(20% 0.025 258);
  --font-display: "Pretendard Variable", "Pretendard", "Noto Sans KR", ui-sans-serif, sans-serif;
  --font-body: "Pretendard Variable", "Pretendard", "Noto Sans KR", ui-sans-serif, sans-serif;
  --font-mono: "SFMono-Regular", "Cascadia Mono", "Roboto Mono", ui-monospace, monospace;
  --spacing-3xs: 0.25rem;
  --spacing-2xs: 0.5rem;
  --spacing-xs: 0.75rem;
  --spacing-sm: 1rem;
  --spacing-md: 1.5rem;
  --spacing-lg: 2rem;
  --spacing-xl: 3rem;
  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-md: 1.125rem;
  --text-lg: 1.375rem;
  --text-xl: 1.75rem;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-in: cubic-bezier(0.7, 0, 0.84, 0);
  --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
  --radius-card: 10px;
  --radius-input: 6px;
}
```

### DTCG `tokens.json`

```json
{
  "$schema": "https://design-tokens.github.io/community-group/format/",
  "color": {
    "paper": { "$value": "oklch(98.3% 0.006 252)", "$type": "color" },
    "paper-2": { "$value": "oklch(96.4% 0.009 252)", "$type": "color" },
    "paper-3": { "$value": "oklch(93.8% 0.012 252)", "$type": "color" },
    "ink": { "$value": "oklch(22% 0.022 258)", "$type": "color" },
    "ink-2": { "$value": "oklch(31% 0.021 258)", "$type": "color" },
    "rule": { "$value": "oklch(88% 0.014 254)", "$type": "color" },
    "accent": { "$value": "oklch(50% 0.2 256)", "$type": "color" },
    "focus": { "$value": "oklch(20% 0.025 258)", "$type": "color" }
  },
  "font": {
    "display": { "$value": "Pretendard Variable, Pretendard, Noto Sans KR, sans-serif", "$type": "fontFamily" },
    "body": { "$value": "Pretendard Variable, Pretendard, Noto Sans KR, sans-serif", "$type": "fontFamily" },
    "mono": { "$value": "SFMono-Regular, Cascadia Mono, Roboto Mono, monospace", "$type": "fontFamily" }
  },
  "space": {
    "xs": { "$value": "0.75rem", "$type": "dimension" },
    "sm": { "$value": "1rem", "$type": "dimension" },
    "md": { "$value": "1.5rem", "$type": "dimension" },
    "lg": { "$value": "2rem", "$type": "dimension" }
  },
  "duration": {
    "micro": { "$value": "120ms", "$type": "duration" },
    "short": { "$value": "220ms", "$type": "duration" },
    "long": { "$value": "420ms", "$type": "duration" }
  }
}
```

### shadcn/ui CSS variables

```css
:root {
  --background: 98.3% 0.006 252;
  --foreground: 22% 0.022 258;
  --card: 98.3% 0.006 252;
  --card-foreground: 22% 0.022 258;
  --popover: 98.3% 0.006 252;
  --popover-foreground: 22% 0.022 258;
  --primary: 50% 0.2 256;
  --primary-foreground: 98.5% 0.006 252;
  --secondary: 93.8% 0.012 252;
  --secondary-foreground: 31% 0.021 258;
  --muted: 88% 0.014 254;
  --muted-foreground: 46% 0.018 257;
  --accent: 50% 0.2 256;
  --accent-foreground: 98.5% 0.006 252;
  --destructive: 47% 0.14 26;
  --destructive-foreground: 98.5% 0.006 252;
  --border: 88% 0.014 254;
  --input: 88% 0.014 254;
  --ring: 20% 0.025 258;
  --radius: 10px;
}
```
