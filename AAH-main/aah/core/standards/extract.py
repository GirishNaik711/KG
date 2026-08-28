#!/usr/bin/env python3
"""
Heuristic extraction of standards rules from unstructured text documents.

Parses .md, .txt, .rst files from the project's knowledge/ folder and extracts
enforceable coding/security/compliance rules without needing an LLM.

Called inline by resolve.py during standards resolution — no user action needed.
Users simply drop text files in knowledge/ and rules are picked up automatically.
"""

from pathlib import Path


def _infer_priority(text: str) -> str:
    """Infer rule priority from language strength."""
    text_lower = text.lower()
    critical_signals = ["must never", "strictly prohibited", "forbidden", "not acceptable", "not permitted", "not allowed"]
    high_signals = ["must", "required", "shall", "always", "never"]
    medium_signals = ["should", "recommended", "prefer"]

    for signal in critical_signals:
        if signal in text_lower:
            return "critical"
    for signal in high_signals:
        if signal in text_lower:
            return "high"
    for signal in medium_signals:
        if signal in text_lower:
            return "medium"
    return "low"


def _infer_category(heading: str, text: str) -> str:
    """Infer rule category from section heading and content."""
    combined = (heading + " " + text).lower()
    category_signals = {
        "security": ["credential", "api key", "secret", "injection", "xss", "csrf", "header", "encrypt"],
        "authentication": ["jwt", "token", "password", "auth", "session", "login", "oauth", "rate limit"],
        "testing": ["test", "coverage", "mock", "integration test", "regression"],
        "performance": ["latency", "timeout", "connection pool", "async", "cache", "index", "p95", "p99"],
        "error_handling": ["exception", "error", "circuit breaker", "retry", "fallback", "timeout"],
        "data_protection": ["pii", "encrypt", "audit log", "gdpr", "scrub", "personally identifiable"],
        "conventions": ["naming", "snake_case", "camelcase", "docstring", "import", "style", "pep"],
        "architecture": ["microservice", "api design", "endpoint", "rest", "grpc"],
        "observability": ["log", "metric", "trace", "monitor", "alert"],
    }
    for category, signals in category_signals.items():
        if any(s in combined for s in signals):
            return category
    return "conventions"


def _is_enforceable(text: str) -> bool:
    """Check if a sentence contains an enforceable requirement.

    Uses strong mandatory language only. Rejects self-referential/meta sentences
    that describe the document itself or its enforcement process.
    """
    text_lower = text.lower()

    # Skip self-referential, contextual, or process-meta sentences.
    skip_signals = [
        # Self-referential (about the document itself)
        "these guidelines", "this document", "these rules", "this policy",
        "this standard", "these standards", "this guide",
        # Contextual/elaboration
        "the following", "as described below", "see section",
        "this includes:", "this applies to", "for example",
        "this threshold", "this means", "in other words",
        # Process-meta (about how rules are enforced, not what to code)
        "should enforce", "should be reviewed", "should be approved",
        "will be blocked", "will be rejected",
    ]
    if any(signal in text_lower for signal in skip_signals):
        return False

    enforceable_signals = [
        "must", "shall", "required", "never", "always", "forbidden",
        "not permitted", "not allowed", "not acceptable", "prohibited",
        "should", "ensure that", "implement rate", "implement circuit",
    ]
    return any(signal in text_lower for signal in enforceable_signals)


# Filenames that are clearly not standards documents — skip auto-extraction
_NON_STANDARDS_FILENAMES = {
    "project-brief", "readme", "changelog", "contributing", "license",
    "todo", "notes", "meeting-notes", "decisions", "adr",
}


def auto_extract_text_file(file_path: Path) -> dict | None:
    """Heuristically extract rules from a text-based document (.md, .txt, .rst).

    Parses headings + paragraphs, identifies enforceable statements, and produces
    a structured dict. Returns None if no rules found or file is not a standards doc.
    """
    # Skip files that are clearly not standards
    stem_lower = file_path.stem.lower()
    if stem_lower in _NON_STANDARDS_FILENAMES:
        return None

    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    if not text or len(text.strip()) < 50:
        return None

    lines = text.split("\n")
    current_heading = "General"
    paragraphs: list[tuple[str, str]] = []  # (heading, paragraph_text)
    current_para: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Detect markdown headings
        if stripped.startswith("#"):
            if current_para:
                paragraphs.append((current_heading, " ".join(current_para)))
                current_para = []
            current_heading = stripped.lstrip("#").strip()
        elif stripped == "":
            if current_para:
                paragraphs.append((current_heading, " ".join(current_para)))
                current_para = []
        else:
            current_para.append(stripped)

    # Flush last paragraph
    if current_para:
        paragraphs.append((current_heading, " ".join(current_para)))

    # Extract enforceable rules
    rules: list[dict] = []
    rule_counter = 0

    for heading, para in paragraphs:
        sentences = [s.strip() for s in para.replace(". ", ".\n").split("\n") if s.strip()]
        enforceable_sentences = [s for s in sentences if _is_enforceable(s) and len(s) >= 30]

        if not enforceable_sentences:
            continue

        # If all enforceable sentences fit in one rule (< 200 chars combined), merge them
        merged = " ".join(enforceable_sentences)
        if len(merged) <= 200:
            rule_counter += 1
            rules.append({
                "id": f"EXTRACTED-{rule_counter:03d}",
                "category": _infer_category(heading, merged),
                "priority": _infer_priority(merged),
                "description": merged,
            })
        else:
            for sentence in enforceable_sentences:
                rule_counter += 1
                rules.append({
                    "id": f"EXTRACTED-{rule_counter:03d}",
                    "category": _infer_category(heading, sentence),
                    "priority": _infer_priority(sentence),
                    "description": sentence,
                })

    if not rules:
        return None

    return {
        "standard_id": f"EXTRACTED-{file_path.stem[:30].upper()}",
        "name": f"Extracted from {file_path.name}",
        "source_type": "company",
        "rules": rules,
    }
