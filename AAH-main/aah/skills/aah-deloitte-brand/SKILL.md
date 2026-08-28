---
name: aah-deloitte-brand
description: "Deloitte's 'Together makes progress' brand guidelines for creating on-brand content across ALL formats. ALWAYS use this skill when: creating presentations, documents, marketing materials, social media posts, emails, web artifacts, dashboards, or any content that should reflect Deloitte's brand platform; applying Deloitte colors, typography, or visual identity; writing in Deloitte's voice or tone; creating HTML/React artifacts with Deloitte branding; building branded dashboards or data visualizations; drafting any Deloitte-related communication (internal or external); the user mentions Deloitte branding, brand guidelines, on-brand, green brand, or professional Deloitte materials. Applies to the June 2025 brand refresh. Even if the user doesn't explicitly mention 'brand', trigger this skill whenever creating Deloitte-facing content."
---

# Deloitte Brand Skill

Create content aligned with Deloitte's *Together makes progress* brand platform (June 2025).

## Workflow

When this skill triggers, follow these steps:

### Step 1: Determine Content Type & Constraints
- **What format?** Presentation, document, email, social post, web artifact, dashboard, marketing material
- **Who is the audience?** C-Suite, practitioners, clients, internal employees
- **Any restrictions?** A&A/Tax regulated entities, sub-brands, co-branded materials
- If creating files, also read the relevant creation skill (pptx, docx, xlsx, frontend-design)

### Step 2: Apply Brand Voice
Answer the storytelling framework before writing:
1. **What is coming together?** (People + tech? Alliances? Disciplines via MDM?)
2. **What progress are we making?** (Incremental or transformational?)

Then write using Deloitte's voice: **Clear, Confident, Human**
- Clarity first — concise, with a point of view
- Insightful beats clever — substance over wordplay
- Be human — conversational, person-to-person
- Genuine optimism — positive, forward-moving
- Meet people where they are — adapt to audience
- Convey purpose — show impact

### Step 3: Apply Visual Identity (if applicable)
- Read `references/visual-details.md` for compositions, motifs, photography
- Read `references/colors-typography.md` for exact hex codes and type specs
- Read `references/digital-brand.md` for HTML/React/dashboard applications

### Step 4: Validate Output
Run the brand compliance check (see **Validation Checklist** below).

---

## Brand Platform Quick Reference

### Tagline
*Together makes progress* — italicized, sentence case, no period when standalone. Never modify, abbreviate, or create variations.

### Core Themes

| Known For | Stand For | Believe In |
|-----------|-----------|------------|
| Multidisciplinary | Purposeful | Solving |
| Collaborative | Advancing | Innovating |
| Convening | Impactful | Creating |

### Alternative Vocabulary (avoid overusing tagline words)

| Instead of "Together" | Instead of "Makes" | Instead of "Progress" |
|-----------------------|--------------------|-----------------------|
| Jointly, collectively, side by side, convening | Creates, shapes, generates, accomplishes | Advance, growth, momentum, transformation |
| MDM, ecosystems, alliances, perspectives | Advise, implement, operate | Outcomes, evolution, impact, scale |

---

## Visual System Summary

### Colors
| Role | Color | Hex |
|------|-------|-----|
| **Primary** | Deloitte Green | #86BC25 |
| **Primary** | Black | #000000 |
| **Accent** | Neon Green | #86EB22 |
| **Accent** | Blue | #00A3E0 |
| **Background** | Dark Gray | #282728 |
| **Background** | White | #FFFFFF |

Gradient palette and full specs → `references/colors-typography.md`

### Typography
- **Primary**: Open Sans (Light / Regular / Bold) — headlines, body, CTAs
- **Accent**: Stix Two Text Semibold Italic — ONE word per headline only, never in body copy
- On dark backgrounds: Open Sans white + Stix Two Text in #86EB22 or gradient
- On light backgrounds: all text one color (black or #282728)

### Circular Motifs
Circles are foundational. Apply through: motif → expression → composition.
- Max 3 visual elements per frame
- Never create two overlapping circles (resembles Mastercard)
- Full specs → `references/visual-details.md`

### Logo Lockup
- **Primary**: Logo + tagline below (general marketing)
- **Secondary**: Tagline subordinate (large format)
- **Untethered**: Tagline separate (merch/events only, requires approval)
- **DO NOT use lockup** for: Restricted Entities, sub-brands, co-branded materials

---

## Content Type Integration

### Presentations → also read pptx skill
- Brand backgrounds (light/dark/green with grain texture)
- Headlines: Open Sans + one Stix Two Text italic word
- Circular motifs for visual interest
- Logo lockup lower right on title slide minimum
- All five brand codes on cover slide

### Documents → also read docx skill
- Cleaner, content-focused application
- Typography follows brand rules
- Circular elements in headers where appropriate
- Check A&A/Tax restrictions before using lockup

### Web Artifacts / Dashboards → also read frontend-design skill
- Full guidance in `references/digital-brand.md`
- Use CSS variables for brand colors
- Open Sans via Google Fonts (or system fallback stack)
- Deloitte green must appear in primary UI elements

### Emails / Social Posts
- Conversational, human tone
- Lead with insight, not promotion
- Genuine optimism about outcomes
- Social: keep visually simple, clear at thumbnail size

### Marketing Materials
- All five brand codes on primary surfaces
- Balance visual impact with information density
- Full examples → `references/examples.md`

---

## Key Restrictions

**NEVER:**
- Modify the tagline words or create variations
- Use multiple italic treatments in one headline
- Use tagline lockup on sub-brands or co-branded materials
- Use Stix Two Text in body copy
- Create two overlapping circles
- Use colors outside approved palette
- Use "TMP" as an acronym for the tagline

**For A&A and Tax:** Check specific guardrails — generally avoid lockup on Restricted Entity documents.

---

## Validation Checklist

Before delivering any Deloitte-branded content, verify:

**Messaging:**
- [ ] Answers "what is coming together?" and "what progress?"
- [ ] Tone is clear, confident, and human
- [ ] Tagline used correctly (if present) — unmodified, italicized, sentence case
- [ ] No overuse of "together," "makes," or "progress" in body copy
- [ ] Audience-appropriate depth and formality

**Visual (if applicable):**
- [ ] Deloitte Green (#86BC25) is prominent
- [ ] Typography follows Open Sans + Stix Two Text rules
- [ ] Max one italic accent word per headline
- [ ] Circular motifs present (no Mastercard pattern)
- [ ] Max 3 visual elements per frame
- [ ] ADA-compliant contrast

**Compliance:**
- [ ] Logo lockup appropriate for content type
- [ ] No lockup on regulated/sub-brand/co-branded materials
- [ ] Colors within approved palette

---

## Reference Files

Load these as needed for detailed specifications:
- **references/messaging-details.md** — Full storytelling guidance, audience adaptation, voice principles
- **references/visual-details.md** — Motifs, compositions, photography, backgrounds, grid system
- **references/colors-typography.md** — Complete hex codes, type specs, background recipes
- **references/digital-brand.md** — CSS/Tailwind implementation for HTML, React, and dashboard artifacts
- **references/examples.md** — Practical examples across presentations, social, email, marketing (includes common scenarios and edge cases)
