# Digital Brand Implementation

How to apply Deloitte's *Together makes progress* brand to HTML artifacts, React components, dashboards, and web-based deliverables.

## Table of Contents
1. [CSS Variables & Color System](#css-variables)
2. [Typography for Web](#typography-for-web)
3. [Tailwind Utility Classes](#tailwind-mapping)
4. [Component Patterns](#component-patterns)
5. [Dashboard Design](#dashboard-design)
6. [Circular Motifs in CSS/SVG](#circular-motifs)
7. [Dark/Light Theme Patterns](#themes)
8. [Common Anti-Patterns](#anti-patterns)

---

## CSS Variables & Color System {#css-variables}

Use CSS custom properties for consistent brand application:

```css
:root {
  /* Primary */
  --dl-green: #86BC25;
  --dl-black: #000000;

  /* Secondary */
  --dl-neon-green: #86EB22;
  --dl-blue: #00A3E0;
  --dl-light-blue: #A0DCFF;
  --dl-blue-dark: #005587;
  --dl-dark-gray: #282728;
  --dl-gray: #E6E6E6;
  --dl-white: #FFFFFF;

  /* Gradients */
  --dl-gradient-green: linear-gradient(135deg, #86BC25, #86EB22);
  --dl-gradient-blue-green: linear-gradient(135deg, #86EB22, #3FC3EC);
  --dl-gradient-full: linear-gradient(135deg, #B7E320, #63C631, #3FC3EC);

  /* Background surfaces */
  --dl-bg-light: #E2E2E2;
  --dl-bg-dark: #282728;

  /* Semantic */
  --dl-text-primary: #282728;
  --dl-text-secondary: #555555;
  --dl-text-on-dark: #E6E6E6;
  --dl-accent: #86BC25;
  --dl-accent-bright: #86EB22;
  --dl-border: #E6E6E6;
  --dl-border-accent: #86BC25;
}
```

### Dark Theme Override
```css
[data-theme="dark"], .dl-dark {
  --dl-text-primary: #E6E6E6;
  --dl-text-secondary: #A0A0A0;
  --dl-accent: #86EB22;
  --dl-border: #444444;
  --dl-bg-surface: #282728;
}
```

---

## Typography for Web {#typography-for-web}

### Font Loading
```html
<link href="https://fonts.googleapis.com/css2?family=Open+Sans:wght@300;400;700&display=swap" rel="stylesheet">
```

Note: Stix Two Text is not available on Google Fonts. For web artifacts, use italic emphasis within Open Sans as an alternative, or apply CSS styling to approximate the accent effect.

### Font Stack
```css
body {
  font-family: 'Open Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-weight: 400;
  color: var(--dl-text-primary);
  line-height: 1.6;
}

h1, h2, h3 {
  font-weight: 700;
  line-height: 1.2;
}

/* Accent word in headlines — replaces Stix Two Text for web */
.dl-accent-word {
  font-style: italic;
  color: var(--dl-accent);
  font-weight: 700;
}

/* On dark backgrounds, accent word uses neon green */
.dl-dark .dl-accent-word {
  color: var(--dl-neon-green);
}
```

### Type Scale
```css
.dl-display  { font-size: 2.5rem; font-weight: 300; }  /* Hero headlines */
.dl-h1       { font-size: 2rem;   font-weight: 700; }
.dl-h2       { font-size: 1.5rem; font-weight: 700; }
.dl-h3       { font-size: 1.25rem; font-weight: 700; }
.dl-body     { font-size: 1rem;   font-weight: 400; }
.dl-caption  { font-size: 0.875rem; font-weight: 400; color: var(--dl-text-secondary); }
```

---

## Tailwind Utility Classes {#tailwind-mapping}

When building React artifacts with Tailwind, use these mappings:

| Brand Element | Tailwind Classes |
|---------------|------------------|
| Deloitte Green text | `text-[#86BC25]` |
| Deloitte Green bg | `bg-[#86BC25]` |
| Neon Green accent | `text-[#86EB22]` |
| Dark background | `bg-[#282728]` |
| Light background | `bg-[#E2E2E2]` |
| Blue accent | `text-[#00A3E0]` |
| Body text | `text-[#282728]` |
| Light text (on dark) | `text-[#E6E6E6]` |
| Green border | `border-[#86BC25]` |
| Card surface | `bg-white rounded-lg shadow-sm border border-[#E6E6E6]` |
| Primary button | `bg-[#86BC25] text-white font-bold px-6 py-2 rounded hover:bg-[#75A521]` |
| Secondary button | `border-2 border-[#86BC25] text-[#86BC25] font-bold px-6 py-2 rounded hover:bg-[#86BC25] hover:text-white` |
| Accent headline word | `italic text-[#86BC25] font-bold` |

### Font Loading in React Artifacts
Add to the top of JSX artifacts:
```jsx
<style>{`@import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@300;400;700&display=swap');`}</style>
```
Then apply `font-family: 'Open Sans', sans-serif` to the root container.

---

## Component Patterns {#component-patterns}

### Header / Navigation Bar
```
┌──────────────────────────────────────────────┐
│ [Deloitte Green bar — 4px top border]        │
│  Logo Area          Nav Items      Actions    │
│  (text or SVG)      (Open Sans)    (buttons)  │
└──────────────────────────────────────────────┘
```
- 4px solid #86BC25 top border (signature green line)
- White or dark gray background
- Open Sans for all nav text
- Green accent on active/hover states

### Metric / KPI Card
```
┌─────────────────────┐
│  Caption label       │  ← dl-caption, text-secondary
│  $4.2M              │  ← dl-h1, text-primary or dl-green
│  ▲ 12% vs. prior    │  ← dl-caption, green for positive
│  ─────────────────  │  ← subtle green accent line
└─────────────────────┘
```
- Use Deloitte Green for positive metrics, Blue for neutral, inherit for negative
- Subtle green bottom border or left border accent
- White card with light shadow on light backgrounds

### Data Table
- Header row: #282728 background with white text, or #86BC25 background with white text
- Alternating rows: white / #F5F5F5
- Sort indicators and interactive elements in Deloitte Green
- Border: 1px #E6E6E6

### Charts and Visualizations
Color sequence for data series:
1. #86BC25 (Deloitte Green) — always first/primary
2. #00A3E0 (Blue)
3. #282728 (Dark Gray)
4. #86EB22 (Neon Green)
5. #A0DCFF (Light Blue)
6. #005587 (Blue Dark)
7. #B7E320 (Bright Lime)
8. #63C631 (Green)

Rules:
- Deloitte Green must be the primary/first data series
- Maintain sufficient contrast between adjacent series
- Use gradients sparingly — solid colors preferred for data accuracy
- Grid lines: #E6E6E6, subtle
- Axis labels: Open Sans Regular, #282728

### Status Indicators
| Status | Color | Hex |
|--------|-------|-----|
| Success / On track | Deloitte Green | #86BC25 |
| Information / In progress | Blue | #00A3E0 |
| Warning / At risk | Amber (supplemental) | #E8A317 |
| Error / Off track | Red (supplemental) | #DA291C |

Note: Amber and red are supplemental — not part of core brand palette but acceptable for functional status indicators in dashboards and data visualizations.

---

## Dashboard Design {#dashboard-design}

### Layout Principles
- Clean grid layout with consistent spacing (16px / 24px base units)
- White or light gray (#F5F5F5) page background
- Cards for content grouping with subtle shadows
- Green accent elements throughout (borders, highlights, active states)
- Deloitte green header bar or top border on every dashboard

### Dashboard Header Pattern
```
┌──────────────────────────────────────────────┐
│ ████████████████████████ (#86BC25 bar)        │
│  Dashboard Title              Filters / Date  │
│  Subtitle or breadcrumb                       │
└──────────────────────────────────────────────┘
```

### Sidebar Navigation (if applicable)
- Dark gray (#282728) sidebar
- White text for labels
- Active item: #86BC25 left border accent + slightly lighter background
- Icons: white, with green for active state

### Information Hierarchy
1. **Primary numbers/KPIs**: Large, bold Open Sans, Deloitte Green or dark text
2. **Section headers**: Open Sans Bold, #282728
3. **Body/details**: Open Sans Regular, #282728
4. **Captions/labels**: Open Sans Regular, smaller, #555555
5. **Interactive elements**: Deloitte Green for links, buttons, active states

---

## Circular Motifs in CSS/SVG {#circular-motifs}

For web artifacts, translate circular brand motifs using CSS or inline SVG:

### CSS Circles
```css
/* Decorative background circle */
.dl-circle-motif {
  position: absolute;
  border-radius: 50%;
  border: 2px solid rgba(134, 188, 37, 0.2);
  pointer-events: none;
}

/* Gradient circle */
.dl-circle-gradient {
  background: linear-gradient(135deg, #86BC25, #86EB22, #3FC3EC);
  border-radius: 50%;
  opacity: 0.1;
}
```

### SVG Concentric Rings (for decorative headers)
```svg
<svg viewBox="0 0 200 200" opacity="0.15">
  <circle cx="100" cy="100" r="90" fill="none" stroke="#86BC25" stroke-width="1"/>
  <circle cx="100" cy="100" r="70" fill="none" stroke="#86EB22" stroke-width="1"/>
  <circle cx="100" cy="100" r="50" fill="none" stroke="#00A3E0" stroke-width="1"/>
</svg>
```

### Application Guidelines
- Circular motifs should be **subtle** in web artifacts — background decoration, not foreground
- Crop circles at edges of containers for dynamic energy
- Use low opacity (0.05–0.2) for background motifs so they don't compete with data
- In dashboards: circular motifs work well in headers or empty states, less so over data

---

## Dark/Light Theme Patterns {#themes}

### Light Theme (Default)
- Page background: #F5F5F5 or #FFFFFF
- Card background: #FFFFFF
- Text: #282728
- Accent: #86BC25
- Borders: #E6E6E6
- Headline accent word: italic, #86BC25

### Dark Theme
- Page background: #282728 or #1A1A1A
- Card background: #333333
- Text: #E6E6E6
- Accent: #86EB22 (neon green — higher visibility on dark)
- Borders: #444444
- Headline accent word: italic, #86EB22

### Key Rule
On dark backgrounds, always shift from Deloitte Green (#86BC25) to Neon Green (#86EB22) for text and accent elements — the standard green lacks sufficient contrast on dark surfaces.

---

## Common Anti-Patterns {#anti-patterns}

**DON'T:**
- Use Deloitte Green as a text color on white without checking contrast (works for large/bold text, fails for small body text — WCAG AA requires 4.5:1 ratio for normal text)
- Apply brand gradient to data visualization bars/lines (confuses data reading)
- Overload dashboards with circular motifs (data clarity > decoration)
- Use the full brand treatment on every internal tool (internal dashboards can be lighter-touch)
- Mix brand greens randomly — #86BC25 is primary, #86EB22 is accent/dark-bg only
- Forget to load Open Sans — falling back to system fonts loses brand recognition

**DO:**
- Always include at least one Deloitte Green element (header bar, accent, button)
- Use the brand color sequence for charts (green first)
- Keep UI clean and data-forward — the brand is "clear, confident, human," not decorative
- Test contrast ratios, especially green text on white/light backgrounds
- Apply circular motifs subtly as background texture, not competing with content
