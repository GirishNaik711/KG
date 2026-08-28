"""Shared test fixtures for codemap-scale."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from codemap_scale.graph.sqlite_graph import SQLiteSymbolGraph


# -------------------------------------------------------------------
# Sample Python repo with known structure
# -------------------------------------------------------------------

@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """
    Create a small Python project with known imports, classes, and calls.

    Structure:
        src/
            __init__.py
            service.py    — PaymentService.process() calls validator.validate()
            validator.py  — Validator.validate() + validate_card()
            utils.py      — format_amount(), log_transaction()
        tests/
            test_service.py
        main.py           — entry point, imports PaymentService
    """
    src = tmp_path / "src"
    src.mkdir()
    tests = tmp_path / "tests"
    tests.mkdir()

    (src / "__init__.py").write_text("")

    (src / "service.py").write_text(textwrap.dedent("""\
        from src.validator import Validator, validate_card
        from src.utils import format_amount, log_transaction


        class PaymentService:
            \"\"\"Handles payment processing.\"\"\"

            def __init__(self, gateway):
                self.gateway = gateway
                self.validator = Validator()

            def process(self, amount, card_number):
                validate_card(card_number)
                formatted = format_amount(amount)
                result = self.gateway.charge(formatted, card_number)
                log_transaction(result)
                return result

            def refund(self, transaction_id, amount):
                return self.gateway.refund(transaction_id, amount)


        def create_service(gateway):
            return PaymentService(gateway)
    """))

    (src / "validator.py").write_text(textwrap.dedent("""\
        import re


        class Validator:
            \"\"\"Validates payment data.\"\"\"

            def validate(self, data):
                if not data.get("card_number"):
                    raise ValueError("Missing card number")
                return True

            def validate_amount(self, amount):
                if amount <= 0:
                    raise ValueError("Amount must be positive")
                return True


        def validate_card(card_number):
            \"\"\"Validate card number using Luhn algorithm.\"\"\"
            if not re.match(r'^[0-9]{13,19}$', card_number):
                raise ValueError("Invalid card number format")
            return True


        def check_expiry(month, year):
            return month >= 1 and month <= 12 and year >= 2024
    """))

    (src / "utils.py").write_text(textwrap.dedent("""\
        import logging

        logger = logging.getLogger(__name__)


        def format_amount(amount):
            return round(float(amount), 2)


        def log_transaction(result):
            logger.info("Transaction: %s", result)


        def generate_id():
            import uuid
            return str(uuid.uuid4())
    """))

    (tests / "__init__.py").write_text("")
    (tests / "test_service.py").write_text(textwrap.dedent("""\
        from src.service import PaymentService


        class TestPaymentService:
            def test_process(self):
                pass

            def test_refund(self):
                pass
    """))

    (tmp_path / "main.py").write_text(textwrap.dedent("""\
        from src.service import PaymentService, create_service


        def main():
            service = create_service(gateway=None)
            result = service.process(100.0, "4111111111111111")
            print(result)


        if __name__ == "__main__":
            main()
    """))

    return tmp_path


@pytest.fixture
def sample_repo_multilang(sample_repo: Path) -> Path:
    """Extends sample_repo with TypeScript and Go files."""
    ts_dir = sample_repo / "frontend"
    ts_dir.mkdir()

    (ts_dir / "index.ts").write_text(textwrap.dedent("""\
        export interface PaymentRequest {
            amount: number;
            cardNumber: string;
        }

        export class PaymentClient {
            async submitPayment(req: PaymentRequest): Promise<void> {
                console.log("submitting", req);
            }
        }

        export function formatCurrency(amount: number): string {
            return `$${amount.toFixed(2)}`;
        }
    """))

    go_dir = sample_repo / "cmd"
    go_dir.mkdir()

    (go_dir / "main.go").write_text(textwrap.dedent("""\
        package main

        import "fmt"

        type Config struct {
            Host string
            Port int
        }

        func main() {
            cfg := Config{Host: "localhost", Port: 8080}
            fmt.Println(cfg)
        }

        func NewConfig(host string, port int) Config {
            return Config{Host: host, Port: port}
        }
    """))

    return sample_repo


# -------------------------------------------------------------------
# Pre-populated graph for query testing
# -------------------------------------------------------------------

@pytest.fixture
def populated_graph(tmp_path: Path) -> SQLiteSymbolGraph:
    """SQLiteSymbolGraph pre-loaded with known data for query testing."""
    db_path = tmp_path / "test.db"
    graph = SQLiteSymbolGraph(db_path)

    # Files
    graph.upsert_files_batch([
        {"path": "src/service.py", "language": "python", "content_hash": "aaa", "size_bytes": 500, "line_count": 30, "tier": 1},
        {"path": "src/validator.py", "language": "python", "content_hash": "bbb", "size_bytes": 400, "line_count": 25, "tier": 1},
        {"path": "src/utils.py", "language": "python", "content_hash": "ccc", "size_bytes": 200, "line_count": 15, "tier": 1},
        {"path": "main.py", "language": "python", "content_hash": "ddd", "size_bytes": 150, "line_count": 10, "tier": 2},
        {"path": "tests/test_service.py", "language": "python", "content_hash": "eee", "size_bytes": 100, "line_count": 8, "tier": 1},
    ])

    # Symbols
    graph.upsert_symbols_batch([
        {"fqn": "src.service.PaymentService", "name": "PaymentService", "kind": "class", "language": "python", "file_path": "src/service.py", "start_line": 5, "end_line": 22, "tier": 1},
        {"fqn": "src.service.PaymentService.process", "name": "process", "kind": "method", "language": "python", "file_path": "src/service.py", "start_line": 12, "end_line": 18, "tier": 1},
        {"fqn": "src.service.PaymentService.refund", "name": "refund", "kind": "method", "language": "python", "file_path": "src/service.py", "start_line": 20, "end_line": 22, "tier": 1},
        {"fqn": "src.service.create_service", "name": "create_service", "kind": "function", "language": "python", "file_path": "src/service.py", "start_line": 24, "end_line": 25, "tier": 1},
        {"fqn": "src.validator.Validator", "name": "Validator", "kind": "class", "language": "python", "file_path": "src/validator.py", "start_line": 4, "end_line": 15, "tier": 1},
        {"fqn": "src.validator.validate_card", "name": "validate_card", "kind": "function", "language": "python", "file_path": "src/validator.py", "start_line": 17, "end_line": 21, "tier": 1},
        {"fqn": "src.utils.format_amount", "name": "format_amount", "kind": "function", "language": "python", "file_path": "src/utils.py", "start_line": 6, "end_line": 7, "tier": 1},
        {"fqn": "src.utils.log_transaction", "name": "log_transaction", "kind": "function", "language": "python", "file_path": "src/utils.py", "start_line": 10, "end_line": 11, "tier": 1},
        {"fqn": "main.main", "name": "main", "kind": "function", "language": "python", "file_path": "main.py", "start_line": 4, "end_line": 8, "tier": 2},
    ])

    # Relations (call graph)
    graph.upsert_relations_batch([
        {"source_fqn": "src.service.PaymentService.process", "target_fqn": "src.validator.validate_card", "kind": "calls", "file_path": "src/service.py", "line": 14},
        {"source_fqn": "src.service.PaymentService.process", "target_fqn": "src.utils.format_amount", "kind": "calls", "file_path": "src/service.py", "line": 15},
        {"source_fqn": "src.service.PaymentService.process", "target_fqn": "src.utils.log_transaction", "kind": "calls", "file_path": "src/service.py", "line": 17},
        {"source_fqn": "main.main", "target_fqn": "src.service.create_service", "kind": "calls", "file_path": "main.py", "line": 5},
        {"source_fqn": "main.main", "target_fqn": "src.service.PaymentService.process", "kind": "calls", "file_path": "main.py", "line": 6},
    ])

    yield graph
    graph.close()


# -------------------------------------------------------------------
# CodeMapScale instance
# -------------------------------------------------------------------

@pytest.fixture
def codemap_instance(sample_repo: Path, tmp_path: Path):
    """CodeMapScale pointed at sample_repo, ready for integration tests."""
    from codemap_scale.orchestrator import CodeMapScale

    db_path = tmp_path / "codemap_test.db"
    cm = CodeMapScale(sample_repo, db_path=db_path, workers=2)
    yield cm
    cm.close()


# -------------------------------------------------------------------
# Session-scoped repo caching for slow tests
# -------------------------------------------------------------------

_REPO_CACHE_DIR = Path.home() / ".cache" / "codemap-bench"


def _clone_repo(name: str, url: str) -> Path:
    """Clone a repo to the cache directory if not already there."""
    dest = _REPO_CACHE_DIR / name
    if dest.exists():
        return dest
    _REPO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=1", url, str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


@pytest.fixture(scope="session")
def flask_repo() -> Path:
    return _clone_repo("flask", "https://github.com/pallets/flask.git")


@pytest.fixture(scope="session")
def django_repo() -> Path:
    return _clone_repo("django", "https://github.com/django/django.git")


@pytest.fixture(scope="session")
def fastapi_repo() -> Path:
    return _clone_repo("fastapi", "https://github.com/fastapi/fastapi.git")
