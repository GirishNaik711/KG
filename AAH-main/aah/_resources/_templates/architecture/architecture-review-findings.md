# Architecture Review Board — Findings

**Project:** {project_name}
**Date:** {date}
**Tier:** {mvp | prod} (poc skips review board)
**Reviewer:** aah-arch-review-board agent (Opus, adversarial)

---

## 1. Review Summary

| Attribute | Value |
|-----------|-------|
| Modules reviewed | {N} |
| Design docs reviewed | {N} |
| Total findings | {N} |
| Decision-level findings | {N} |
| Design-level findings | {N} |
| Verdict | {PASS / Rework-Required} |
| Rework cycles used | {0-2} / 2 max |

---

## 2. Review Lenses Applied

| Lens | Question | Result |
|------|----------|--------|
| Module demoability | Are all modules still independently demoable after detailed design? | {pass / findings below} |
| Doc-module consistency | Do design docs contradict module-map boundaries or dependencies? | {pass / findings below} |
| Data-model shared-core | Does the shared-core (from P6) hold under detailed data design? | {pass / findings below} |
| Flow-isolation survival | Do P5–P10 flow-isolation decisions survive Stage 3 detailed design? | {pass / findings below} |

---

## 3. Findings

### Finding F-{001}

| Attribute | Value |
|-----------|-------|
| **Level** | {decision-level / design-level} |
| **Severity** | {critical / major / minor} |
| **Lens** | {which review lens caught this} |
| **Artifact** | {which doc or module-map entry} |
| **Description** | {what the contradiction/issue is} |
| **Evidence** | {specific text/field that conflicts} |
| **Resolution action** | {see §4 below} |

---

## 4. Resolution Actions

### Decision-level findings → User resolution gate

| Finding | Registry Slug | Action Required | Status |
|---------|--------------|-----------------|--------|
| F-{001} | {slug to reopen} | User must re-decide: {question} | {pending / resolved / accepted-tech-debt} |

### Design/module-level findings → Synthesis agent patch

| Finding | Target Artifact | Patch Description | Status |
|---------|----------------|-------------------|--------|
| F-{002} | {doc or module-map} | {what the agent changes} | {patched / accepted-tech-debt} |

---

## 5. Rework Cycle Log

### Cycle 1 (if applicable)

| Finding | Action Taken | Re-check Result |
|---------|--------------|-----------------|
| | | {resolved / still-open} |

### Cycle 2 (if applicable)

| Finding | Action Taken | Re-check Result |
|---------|--------------|-----------------|
| | | {resolved / accepted-tech-debt} |

---

## 6. Accepted Tech Debt

<!-- Findings that survived 2 rework cycles — logged as known debt, not blockers. -->

| Finding | Reason Accepted | Impact | Revisit Trigger |
|---------|----------------|--------|-----------------|
| | {2 cycles exhausted / user accepted} | {what could go wrong} | {when to re-evaluate} |

---

## 7. Final Verdict

| Attribute | Value |
|-----------|-------|
| Verdict | {PASS / PASS-with-tech-debt} |
| Blocking findings remaining | {0} |
| Tech debt items logged | {N} |
| Ready for hand-off to /aah-plan | {yes / no} |
