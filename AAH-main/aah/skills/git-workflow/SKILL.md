---
name: git-workflow
description: Branching strategy and worktree conventions for AAH projects
user-invocable: false
---

# Git Workflow

## Branch Hierarchy

```
main                    ← production-ready only, gated merge
  └─ develop            ← integration branch
       └─ integration/wave-N   ← temporary, for cumulative testing
            └─ feature/FXXX    ← per-feature worktree branches
```

## Branch Rules

### main
- Protected branch — merge requires explicit validation via `/aah-promote-to-main`
- All features must pass, regression suite must pass, no in-progress work
- Fast-forward merge only from develop

### develop
- Integration branch receiving promoted code from integration branches
- Receives fast-forward merges from `integration/wave-N` after regression passes
- Never force-pushed

### integration/wave-N
- Temporary branch created when merging a wave's worktrees
- Created from develop, receives merges from feature branches
- Deleted after successful promotion to develop
- Cumulative regression runs here before promotion

### feature/FXXX (worktree branches)
- Created automatically by aah-feature-implementer subagent with `isolation: worktree`
- Branches off develop
- One feature per worktree — isolated development
- Merged into integration/wave-N when feature passes

## Commit Conventions

- Commit after every meaningful change
- Descriptive messages: what changed and why
- Never leave uncommitted work at end of feature/session
- Format: `feat(FXXX): description` or `fix(FXXX): description`

## Worktree Lifecycle

1. Feature assigned → worktree created automatically
2. Implementation + tests → commits in worktree
3. Feature passes → worktree branch merged to integration
4. Integration tests pass → worktree cleaned up
