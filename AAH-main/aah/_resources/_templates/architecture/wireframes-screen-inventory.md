# UI Wireframes & Screen Inventory

**Project:** {project_name}
**Date:** {date}
**Produced by:** /design-discover port (post-module-finalization)

<!-- One doc covers all modules with frontend. The Screen Inventory maps each
     screen to its module; wireframe sections are grouped per module. -->

---

## 1. Screen Inventory

| Screen ID | Screen Name | Route/Path | Module | Primary Action | Auth Required |
|-----------|-------------|------------|--------|----------------|---------------|
| S-001 | | | | | |
| S-002 | | | | | |

---

## 2. Navigation & Information Architecture

<!-- High-level nav model: how screens connect, primary navigation paths, global vs contextual nav. -->

```
{nav structure — e.g. sidebar items, tab groups, breadcrumb hierarchy}
```

### Nav Hierarchy

| Level | Item | Target Screen | Visible When |
|-------|------|---------------|--------------|
| L1 | | | |
| L2 | | | |

---

## 3. Per-Screen Wireframes

### Screen: {screen_name} (S-001)

**Purpose:** {one sentence — what the user accomplishes here}
**Route:** {/path}
**Entry points:** {how user gets here — nav item, redirect, deep link}

#### Layout Spine

<!-- ASCII wireframe showing regions. Tag each region with [R1], [R2], etc. -->

```
┌─────────────────────────────────────────────────┐
│  [R1] Header / Nav                              │
├──────────────┬──────────────────────────────────┤
│  [R2] Side   │  [R3] Main Content              │
│              │                                  │
│              │  [R4] Action Area                │
│              │                                  │
├──────────────┴──────────────────────────────────┤
│  [R5] Footer / Status                           │
└─────────────────────────────────────────────────┘
```

#### Region Details

| Region | Content | Component Type | Data Source | States |
|--------|---------|----------------|-------------|--------|
| R1 | | | | |
| R2 | | | | |
| R3 | | | | |

#### Data Dependencies

| Data Needed | API Endpoint | Loading State | Empty State | Error State |
|-------------|-------------|---------------|-------------|-------------|
| | | | | |

#### Agentic Surfaces (if applicable)

| Region | Agent Interaction | Trigger | Output Display |
|--------|-------------------|---------|----------------|
| | | | |

---

## 4. User Flow Diagram

<!-- Sequence of screens for primary task completion. Show decision points. -->

```
[Screen A] --action--> [Screen B] --success--> [Screen C]
                                   --error----> [Screen A (error state)]
```

---

## 5. Required States Per Screen

| Screen | Default | Loading | Empty | Error | Success |
|--------|---------|---------|-------|-------|---------|
| S-001 | | | | | |
| S-002 | | | | | |

---

## 6. Component Hierarchy

<!-- Top-level component tree that maps to the wireframe regions. -->

```
App
├── Layout
│   ├── Header [R1]
│   ├── Sidebar [R2]
│   └── Main [R3]
│       ├── {PageComponent}
│       └── {ActionArea} [R4]
└── Footer [R5]
```

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Screen Inventory | {Inherited / Authored} | {e.g. module-map.yaml UI layers} |
| Navigation & Information Architecture | {Inherited / Authored} | {source or arch-phase} |
| Per-Screen Wireframes | {Inherited / Authored} | {source or arch-phase} |
| User Flow Diagram | {Inherited / Authored} | {source or arch-phase} |
| Required States Per Screen | {Inherited / Authored} | {source or arch-phase} |
| Component Hierarchy | {Inherited / Authored} | {source or arch-phase} |
