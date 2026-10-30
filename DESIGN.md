# Vivekium design system

**Status:** current. **Reviewed:** 2026-10-17 against the owner-supplied Vivekium HTML palette, `frontend/app/globals.css`, `frontend/tailwind.config.ts` and the shared brand assets.

This is the visual contract for the public site and every product portal. It is
current-state documentation only. Superseded palettes, logos and typography
experiments are recorded in [docs/history/SUPERSEDED.md](docs/history/SUPERSEDED.md).

## 1. Direction

Vivekium is calm, evidence-led enterprise software. The interface uses warm
paper, dark navy structure and restrained teal evidence accents. Information
hierarchy comes from spacing, type and borders, not decorative effects.

The system rejects:

- purple, violet and indigo;
- multicolour gradients and glowing colour fields;
- decoration that competes with candidate evidence;
- excessive rounding, floating card stacks and heavy shadows;
- colour as the only carrier of status.

## 2. Reference palette

The owner-supplied 2026-10-17 HTML reference defines these exact light-theme
anchors. The HSL values in `frontend/app/globals.css` must resolve to these
hex values.

| Role | CSS token | Hex | Use |
|---|---|---|---|
| Paper | `--canvas` | `#F6F2EB` | Page background |
| Card | `--surface` | `#FFFCF7` | Cards, popovers, sheets |
| Ink | `--ink`, `--navy-600` | `#1B2B4B` | Primary text, structure, primary actions |
| Soft ink | `--ink-soft` | `#24344F` | Hover and secondary structure |
| Muted ink | `--muted-ink` | `#5B6578` | Supporting text |
| Line | `--border-token` | `#DDD6CB` | Dividers and passive borders |
| Evidence teal | `--teal-600` | `#0F6B5C` | Evidence, citations, the logo centre |
| High evidence | `--rating-1-bg` | `#DCEFE6` | Strong positive evidence surface |
| Matching evidence | `--rating-2-bg` | `#E7EFE6` | Positive evidence surface |
| Moderate evidence | `--rating-3-bg` | `#F3EAD3` | Qualified evidence surface |
| Neutral surface | `--muted` | `#EDE7E0` | Quiet controls and disabled context |

White is permitted for control interiors and text on dark ink. Red, amber and
green remain available only for destructive, warning and operational states.
They are not brand colours.

### Semantic rules

- Navy is structure: navigation, primary actions, headings and frames.
- Teal is evidence: verified facts, citations and the central evidence point.
- Warm paper is the default field. Pure white never replaces the page canvas.
- Muted text uses `#5B6578`; it must still pass WCAG AA on its surface.
- Interactive boundaries may use a darker palette token when `#DDD6CB` would
  fall below WCAG 1.4.11. This is an accessibility adaptation, not a new hue.

The optional dark theme is a black field, not a navy inversion. The page is
`#000000`. Cards, menus and the sidebar use a near-black neutral surface so
layers stay separable. Text is `#FFFFFF`. Primary buttons are `#FFFFFF` with
black labels. Secondary actions keep a black fill, white text and a visible
border. Teal remains evidence. The code editor keeps a navy keyword and a teal
literal so syntax stays readable; that blue is not page chrome. Light mode is
the reference presentation and keeps navy as structure.

## 3. Brand mark

The Vivekium mark is four navy directional chevrons around one teal evidence
point. The shared vector geometry is used in the header lockup, browser icon,
Apple touch icon and social card. Do not embed the supplied raster references
in the product and do not restore their purple centre.

The wordmark reads `Vivekium`. Product-facing copy must not use ReadyPick as a
brand name. Infrastructure identifiers may retain the legacy name until they
are migrated independently.

## 4. Typography

Typography is unchanged by the 2026-10-17 palette ruling.

| Role | Face | Use |
|---|---|---|
| Heading and wordmark | Fraunces | Semantic `h1` to `h6`, display titles, section titles, wordmark |
| Body and interface | Outfit | Navigation, controls, prose, forms, tables, metadata |
| Reference | Geist Mono | Codes, identifiers, filenames, URLs and code |

Fonts are self-hosted through `next/font`. No runtime font CDN is allowed.
Use semantic type roles from `frontend/tailwind.config.ts` and
`frontend/app/globals.css`; do not invent component-local font sizes.

## 5. Shape, depth and spacing

- Controls use the shared radius scale, normally 4 to 8 pixels.
- Large cards may use 12 pixels. Pills are reserved for compact status chips.
- Cards use `--surface`, one quiet border and little or no shadow.
- Elevation comes from surface contrast before shadow.
- Spacing follows the Tailwind scale. Product surfaces stay denser than the
  public site because recruiters scan; candidate assessment surfaces stay calm
  and mobile-first because candidates read and answer.

## 6. Components

### Buttons

Primary buttons use navy with white text in light mode, and white with black
text in dark mode. Secondary buttons use a warm surface, navy text and a
visible border in light mode; in dark mode they use a black fill, white text
and a visible border. Teal is not a generic call-to-action colour.
Every state keeps a visible focus ring and a minimum 44 pixel touch target where
the surrounding layout permits it.

### Fields

Fields use a warm or white interior with an explicit boundary. Idle, hover,
focus, error and disabled states must remain distinguishable without relying on
placeholder text. Labels are always visible.

### Cards and tables

Cards group one concept. A border does not justify a card by itself. Tables use
real headers, stable alignment and quiet row separators. Dense data stays dense;
mobile layouts expose the same information without horizontal page overflow.

### Evidence and status

Evidence surfaces use the high, matching and moderate fills from the reference
palette. Each state also carries text or an icon. Destructive and warning
states use their semantic ramps and never masquerade as evidence grades.

## 7. Layout and responsive behaviour

- Public pages use a maximum readable content width and generous section gaps.
- Product pages prioritise the current task and the next permitted action.
- Candidate assessment is one-column and mobile-first.
- Recruiter tables may become stacked records on narrow screens, but no field
  or action may disappear solely because the viewport is small.
- Verify at 320, 768, 1024 and 1440 pixels.

## 8. Motion and interaction

Motion explains state change. Use short opacity, colour and small positional
transitions. Avoid decorative looping motion, spring overshoot, parallax and
large transforms. Respect `prefers-reduced-motion`.

Keyboard focus is visible on every interactive element. Dialogs restore focus,
menus support arrow-key behaviour through their primitives, and async states
announce useful progress or failure text.

## 9. Accessibility and enforcement

Normal text must meet WCAG AA at 4.5:1. Large text and non-text controls must
meet 3:1. `frontend/scripts/check-contrast.mjs` enforces the token pairs. The
typography contract is enforced by `frontend/lib/typography.test.ts`.

Before release:

1. Run `npm run contrast` in `frontend/`.
2. Run the frontend test suite and production build.
3. Inspect public and authenticated surfaces at mobile and desktop widths.
4. Confirm the rendered logo, browser icon and social card use navy and teal.
5. Search production source for retired purple literals and product-facing
   ReadyPick copy.
