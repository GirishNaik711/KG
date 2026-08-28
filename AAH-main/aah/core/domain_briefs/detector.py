#!/usr/bin/env python3
"""Offline keyword-based detector for industry-domain nodes.

Scores every leaf node in the taxonomy against a problem statement plus
intake answers. Pure Python, no network. Used as a fallback when the LLM
classifier is unavailable and as input-candidate selection for the
classifier.

Scoring: strong-keyword match = 3 points, moderate-keyword match = 1 point.
Deeper nodes break ties over shallower ancestor nodes when scores equal.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from aah.core.common.io_utils import read_yaml
from aah.core.domain_briefs.loader import (
    find_briefs_root,
    list_all_ids,
    load_taxonomy,
)


STRONG_WEIGHT = 3
MODERATE_WEIGHT = 1


def _normalize(text: str) -> str:
    return text.lower()


def _count_kw_hits(text: str, keywords: list[str]) -> int:
    """Count distinct keyword hits; each keyword counts at most once."""
    if not keywords:
        return 0
    hits = 0
    for kw in keywords:
        kw_norm = kw.lower()
        # Use word-boundary-ish matching to avoid partials ("aml" inside "flame")
        pattern = r"(?:^|[^a-z0-9])" + re.escape(kw_norm) + r"(?:[^a-z0-9]|$)"
        if re.search(pattern, text):
            hits += 1
    return hits


def _score_node(text: str, node: dict) -> int:
    keywords = node.get("keywords") or {}
    strong = keywords.get("strong") or []
    moderate = keywords.get("moderate") or []
    return (
        _count_kw_hits(text, strong) * STRONG_WEIGHT
        + _count_kw_hits(text, moderate) * MODERATE_WEIGHT
    )


def _load_node_own_content(node_id: str) -> Optional[dict]:
    """Load the node's own YAML (NOT merged with ancestors).

    Needed for detector scoring so leaves don't inherit ancestor keywords.
    """
    root = find_briefs_root()
    if root is None:
        return None
    taxonomy = load_taxonomy()

    def find_file(nodes: list) -> Optional[str]:
        for n in nodes:
            if n.get("id") == node_id:
                return n.get("file")
            result = find_file(n.get("children") or [])
            if result:
                return result
        return None

    file_rel = find_file(taxonomy.get("industries") or [])
    if not file_rel:
        return None
    path = root / file_rel
    if not path.is_file():
        return None
    return read_yaml(path)


def rank_candidates(
    problem_statement: str,
    intake_answers: Optional[list[str]] = None,
    top_k: int = 5,
    min_score: int = 1,
) -> list[dict]:
    """Rank nodes (leaf and intermediate) by OWN-keyword match.

    Uses each node's own keywords (not ancestor-merged) so a statement
    matching only parent-level keywords lands on the parent, not on a
    random child. Deeper nodes break ties when scores are equal.

    Returns list of {id, name, score, depth} sorted by score desc, then
    depth desc. Empty list if no node scores >= min_score.
    """
    if find_briefs_root() is None:
        return []

    text = _normalize(problem_statement or "")
    if intake_answers:
        text += " " + " ".join(_normalize(a or "") for a in intake_answers)

    scored: list[dict] = []
    for nid in list_all_ids():
        own = _load_node_own_content(nid)
        if own is None:
            continue
        # Belt-and-suspenders: list_all_ids() already drops stubs via the taxonomy
        # index, but also honor a stub flag on the node's own file in case the
        # index drifted. Stub nodes contribute no overlay value.
        if own.get("stub"):
            continue
        score = _score_node(text, own)
        if score < min_score:
            continue
        depth = nid.count("/") + 1
        scored.append(
            {
                "id": nid,
                "name": own.get("name", nid),
                "score": score,
                "depth": depth,
            }
        )

    scored.sort(key=lambda r: (r["score"], r["depth"]), reverse=True)
    return scored[:top_k]


def _read_intake_answers(project_path: Path) -> tuple[str, list[str]]:
    """Extract (problem_statement, answers) from a project's intake.json."""
    intake_path = project_path / ".aah" / "intake.json"
    if not intake_path.is_file():
        return "", []
    import json as _json

    intake = _json.loads(intake_path.read_text(encoding='utf-8'))
    problem = intake.get("problem_statement") or ""
    answers: list[str] = []
    for rnd in intake.get("rounds") or []:
        for qa in rnd.get("questions") or []:
            if isinstance(qa, dict):
                a = qa.get("answer")
                if isinstance(a, str):
                    answers.append(a)
    return problem, answers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-path",
        type=Path,
        help="Project directory (reads .aah/intake.json)",
    )
    parser.add_argument(
        "--problem-statement",
        help="Direct problem statement (overrides --project-path)",
    )
    parser.add_argument(
        "--answer",
        action="append",
        default=[],
        help="Additional context (repeatable)",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=int, default=1)
    args = parser.parse_args()

    if args.problem_statement is not None:
        problem = args.problem_statement
        answers = args.answer
    elif args.project_path:
        problem, answers = _read_intake_answers(args.project_path)
        answers += args.answer
    else:
        parser.error("provide --problem-statement or --project-path")

    results = rank_candidates(problem, answers, top_k=args.top_k, min_score=args.min_score)
    json.dump({"candidates": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
