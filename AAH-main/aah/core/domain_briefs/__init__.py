"""Industry-domain taxonomy + Domain Briefs.

This module manages the hierarchical industry-domain taxonomy at
domain-briefs/ in the repo root. Unlike the tech-domain (Build Playbook)
module, Domain Briefs have a parent/child structure with ancestor-merge
inheritance.

Entry points:
  - loader.load_taxonomy() — parse _taxonomy.yaml
  - loader.load_node(node_id) — load a node with merged ancestor fields
  - detector.rank_candidates(intake) — offline keyword-based candidate ranking
  - classifier.classify(intake) — LLM-based classification (Claude Opus)
  - context.build_domain_context_summary(project_path) — SessionStart injection
  - context.get_section(project_path, section) — on-demand full-section retrieval
  - manager — CRUD operations for /aah-domain-briefs skill
  - validate — library integrity checks
  - status — show attached domain for current project
"""

from aah.core.domain_briefs.loader import (
    find_briefs_root,
    load_taxonomy,
    load_node,
    list_leaf_ids,
    list_all_ids,
    get_ancestors,
    is_stub,
)
from aah.core.domain_briefs.detector import rank_candidates
from aah.core.domain_briefs.context import (
    build_domain_context_summary,
    get_section,
)

__all__ = [
    "find_briefs_root",
    "load_taxonomy",
    "load_node",
    "list_leaf_ids",
    "list_all_ids",
    "get_ancestors",
    "is_stub",
    "rank_candidates",
    "build_domain_context_summary",
    "get_section",
]
