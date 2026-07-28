# Design — Lecture STT

A locked design system for the Lecture STT operator console. Every web-panel
screen reads this file before visual changes are made. Extend this system when a
new UI need appears; do not invent a separate per-screen theme.

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

`Cobalt`, adapted for a Korean operational product without network font
dependencies.

- `--color-paper`: `oklch(98.3% 0.006 252)`
- `--color-paper-2`: `oklch(96.4% 0.009 252)`
- `--color-paper-3`: `oklch(93.8% 0.012 252)`
- `--color-ink`: `oklch(22% 0.022 258)`
- `--color-ink-2`: `oklch(31% 0.021 258)`
- `--color-rule`: `oklch(88% 0.014 254)`
- `--color-accent`: `oklch(50% 0.2 256)`
- `--color-accent-ink`: `oklch(98.5% 0.006 252)`
- `--color-focus`: `oklch(20% 0.025 258)`

Cobalt is a signal, not a surface: active navigation, primary guarded actions,
focus, and selected rows should keep it below roughly 3% of a viewport.
Green, amber, and red are reserved for actual semantic states and never used as
decoration. Dark mode keeps the same hue relationships and raises elevated
surfaces by lightness.

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

The source scale lives in `frontend/web-panel/tokens.css` and follows a four
pixel rhythm. Layout and component CSS use named tokens such as
`var(--space-md)` rather than new raw spacing values.

## Layout

- Desktop: command rail and main workbench form an intentionally unequal grid.
- The top runtime strip keeps identity, connection mode, last update, notice,
  notifications, theme, and runtime state together without becoming a hero.
- Equal card grids are avoided. Each screen chooses the surface suited to its
  evidence: ledger, queue, definition list, table, or queue/detail split.
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

- Primary: solid cobalt, 6 px radius, explicit verb naming the existing guarded
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
- Cobalt accent placement and the light/dark palette relationship.
- Pretendard and telemetry-mono roles.
- Button height, radius, focus treatment, and guarded-action voice.
- Left-aligned screen heading rhythm with no decorative ordinal eyebrow.
- Loading, empty, error, disabled, and selected state vocabulary.

## What pages MAY differ on

- Workbench density and the split between overview, queue, detail, and evidence.
- Whether the screen uses a ledger, table, definition grid, or log surface.
- The amount of contextual copy needed to explain a fail-closed boundary.

## Protected behavior

Visual work must not change hash routes, API or DTO contracts, polling/SSE
behavior, write guards, confirmation defaults, immutable storage keys, file
paths, or operating data. It must not add upload, rename/move, automatic
materialization, migration, worker restart, or launchd controls.

## Exports

`frontend/web-panel/tokens.css` is the runtime source of truth. The excerpts
below are portable translations of its core system.

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
