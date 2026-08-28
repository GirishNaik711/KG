#!/usr/bin/env python3
"""
Data Readiness Validation Gate.

Validates that databases and storage services (already confirmed reachable by
cloud-readiness) contain the schema, tables, seed data, and files required for
development to begin.

This gate does NOT modify any data  -- it is strictly read-only validation.
No Alembic, no migrations, no writes.

Sources for expected state (priority order):
1. .aah/architecture/data-readiness.yaml (explicit user-provided expectations)
2. knowledge/ folder (greenfield) or .aah/codebase-intel/ (brownfield)
    -- schema files (.sql, .prisma, .graphql), ERD docs, seed manifests
3. User-prompted (if neither 1 nor 2 provides enough info)

Exit codes:
  0  -- all checks pass (or skipped/overridden)
  2  -- critical failures present
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aah.core.common.config import resolve_project_path
from aah.core.common.io_utils import read_yaml, write_yaml
from aah.core.cloud.identity import (
    aws_identity,
    gcp_identity,
    azure_identity,
    discover_aws_region,
    discover_gcp_project,
    print_identity_banner,
)


# ==============================================================================
# Handshake gate — /aah-data → /aah-access dependency check
# ==============================================================================

def _check_cloud_gate_handshake(project_path: Path) -> None:
    """
    Verify /aah-access has emitted a passing cloud-readiness verdict.

    Halts the process with a clear message if:
      - The file is missing (user hasn't run /aah-access yet).
      - gate_status is not "passed" (the cloud gate failed or was rejected).
      - The file is older than .aah/discuss/decision-registry.yaml
        (the underlying decisions have changed since the last cloud check).

    Called only when --require-cloud-gate is passed (i.e. from /aah-data).
    Existing callers (Analyze-phase orchestrator) omit the flag and are unaffected.
    """
    cloud_yaml = project_path / ".aah" / "architecture" / "cloud-readiness.yaml"
    discuss_yaml = project_path / ".aah" / "discuss" / "decision-registry.yaml"

    if not cloud_yaml.exists():
        print(json.dumps({
            "error": "handshake-failed",
            "reason": "cloud-readiness.yaml not found",
            "next_step": "Run /aah-access first to validate cloud connectivity."
        }))
        sys.exit(2)

    verdict = read_yaml(cloud_yaml) or {}
    status = verdict.get("gate_status")
    if status not in ("passed", "skipped"):
        print(json.dumps({
            "error": "handshake-failed",
            "reason": f"cloud-readiness gate_status={status!r}",
            "next_step": "Fix cloud-readiness failures via /aah-access, then rerun /aah-data."
        }))
        sys.exit(2)

    if discuss_yaml.exists():
        if cloud_yaml.stat().st_mtime < discuss_yaml.stat().st_mtime:
            print(json.dumps({
                "error": "handshake-stale",
                "reason": "cloud-readiness.yaml is older than decision-registry.yaml",
                "next_step": "Rerun /aah-access — the underlying decisions have changed."
            }))
            sys.exit(2)


# ==============================================================================
# Service type classification
# ==============================================================================
# DATABASE_SERVICE_TYPES, STORAGE_SERVICE_TYPES, and CHECK_DISPATCH are derived
# from _DATA_SERVICE_REGISTRY (defined below the handler functions, near
# State management). Adding a new data-bearing service is one edit there.

def _is_database(service_type: str) -> bool:
    return service_type in DATABASE_SERVICE_TYPES


def _is_storage(service_type: str) -> bool:
    return service_type in STORAGE_SERVICE_TYPES


def _is_api(service_type: str) -> bool:
    return service_type in API_SERVICE_TYPES


# ==============================================================================
# Schema discovery from knowledge / codebase-intel
# ==============================================================================

SCHEMA_FILE_EXTENSIONS = {".sql", ".prisma", ".graphql", ".gql", ".dbml"}
SCHEMA_FILE_PATTERNS = ["schema", "migration", "create_table", "ddl", "init"]


def discover_schema_sources(project_path: Path) -> list[dict]:
    """
    Find schema definition files in knowledge/.
    Returns list of {path, source, file_type, tables_mentioned, columns}.
    """
    sources = []

    from aah.core.knowledge.parser import find_knowledge_dir
    knowledge_dir = find_knowledge_dir(project_path)

    search_dirs = []
    if knowledge_dir and knowledge_dir.is_dir():
        search_dirs.append(("knowledge", knowledge_dir))

    for source_label, search_dir in search_dirs:
        for f in sorted(search_dir.rglob("*")):
            if not f.is_file():
                continue
            suffix = f.suffix.lower()
            name_lower = f.stem.lower()

            is_schema = (
                suffix in SCHEMA_FILE_EXTENSIONS
                or any(p in name_lower for p in SCHEMA_FILE_PATTERNS)
            )
            if not is_schema:
                continue

            tables = _extract_table_names(f) if suffix == ".sql" else []
            columns = _extract_table_columns(f) if suffix == ".sql" else {}
            sources.append({
                "path": str(f.relative_to(project_path)),
                "source": source_label,
                "file_type": suffix.lstrip("."),
                "tables_mentioned": tables,
                "columns": columns,
            })

    return sources


# ==============================================================================
# Live discovery — when no schema files exist in knowledge/
# ==============================================================================

def discover_project_keywords(project_path: Path) -> list[str]:
    """
    Read knowledge/ for project context (process names, domain terms) so we
    can suggest table-name matches even without a schema file. Returns a
    deduplicated list of lowercase keywords longer than 3 chars.
    """
    keywords: set[str] = set()

    try:
        from aah.core.knowledge.parser import find_knowledge_dir
        knowledge_dir = find_knowledge_dir(project_path)
    except Exception:
        knowledge_dir = None

    candidate_files = []
    if knowledge_dir and knowledge_dir.is_dir():
        for f in knowledge_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in {".md", ".yaml", ".yml", ".txt"}:
                candidate_files.append(f)

    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    if registry_path.exists():
        try:
            registry = read_yaml(registry_path)
            ctx = registry.get("context", {}) or {}
            domain = ctx.get("domain_scope", {}) or {}
            for proc in domain.get("processes", []) or []:
                name = proc if isinstance(proc, str) else proc.get("name", "")
                for token in str(name).lower().replace("-", "_").replace(" ", "_").split("_"):
                    if len(token) > 3:
                        keywords.add(token)
            for integ in ctx.get("integrations", []) or []:
                name = integ if isinstance(integ, str) else integ.get("system", "")
                for token in str(name).lower().split():
                    if len(token) > 3:
                        keywords.add(token)
        except Exception:
            pass

    import re
    word_re = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")
    for f in candidate_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in word_re.finditer(text):
            tok = m.group(0).lower()
            if tok not in {"this", "that", "with", "from", "they", "have",
                           "been", "were", "will", "should", "would", "knowledge"}:
                keywords.add(tok)

    return sorted(keywords)


def list_live_tables_postgres(config: dict) -> list[str]:
    """
    Return all base tables in the live database (read-only).
    Used by live-discovery mode when no schema file is available.
    """
    schema = config.get("schema", "public")
    connection_name = config.get("connection_name", "")
    if connection_name:
        try:
            from google.cloud.sql.connector import Connector
            import pg8000  # noqa: F401
        except ImportError:
            return []
        connector = None
        conn = None
        try:
            connector = Connector()
            kwargs = {"user": config.get("username"), "db": config.get("database")}
            if config.get("auth_method") == "secret-manager":
                pw, err = _fetch_password_from_gcp_secret(config.get("secret_path", ""))
                if err:
                    return []
                kwargs["password"] = pw
                kwargs["enable_iam_auth"] = False
            else:
                kwargs["enable_iam_auth"] = True
            conn = connector.connect(connection_name, "pg8000", **kwargs)
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (schema,),
            )
            rows = cur.fetchall()
            cur.close()
            return [r[0] for r in rows]
        except Exception:
            return []
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            try:
                if connector is not None:
                    connector.close()
            except Exception:
                pass
    else:
        try:
            import psycopg2
        except ImportError:
            return []
        endpoint = config.get("endpoint") or config.get("hostname", "")
        if not endpoint or not config.get("database") or not config.get("username"):
            return []
        params = {}
        if ":" in endpoint:
            host, port_str = endpoint.rsplit(":", 1)
            params["host"] = host
            params["port"] = int(port_str)
        else:
            params["host"] = endpoint
            params["port"] = config.get("port", 5432)
        params["dbname"] = config["database"]
        params["user"] = config["username"]
        if config.get("password"):
            params["password"] = config["password"]
        params["connect_timeout"] = 10
        try:
            conn = psycopg2.connect(**params)
            conn.set_session(readonly=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                (schema,),
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return [r[0] for r in rows]
        except Exception:
            return []


def suggest_use_case_tables(live_tables: list[str], keywords: list[str]) -> list[dict]:
    """
    Score each live table by how many project keywords appear in its name.
    Returns [{table, matched_keywords, score}] sorted by score desc.
    Score 0 entries are still returned so the user can see the full list.
    """
    suggestions = []
    for t in live_tables:
        t_lower = t.lower()
        matched = [k for k in keywords if k in t_lower]
        suggestions.append({
            "table": t,
            "matched_keywords": matched,
            "score": len(matched),
        })
    suggestions.sort(key=lambda s: (-s["score"], s["table"]))
    return suggestions


def _extract_table_names(sql_path: Path) -> list[str]:
    """Extract table names from CREATE TABLE statements in a .sql file."""
    import re
    tables = []
    try:
        content = sql_path.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`|\")?(\w+)(?:`|\")?",
            re.IGNORECASE,
        )
        tables = [m.group(1) for m in pattern.finditer(content)]
    except Exception:
        pass
    return tables


def _extract_table_columns(sql_path: Path) -> dict[str, list[dict]]:
    """
    Extract table -> columns from CREATE TABLE statements.
    Returns {table_name: [{name, type}]}.
    """
    import re
    result = {}
    try:
        content = sql_path.read_text(encoding="utf-8", errors="replace")
        table_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`|\")?(\w+)(?:`|\")?\s*\((.*?)\)",
            re.IGNORECASE | re.DOTALL,
        )
        col_pattern = re.compile(
            r"^\s*(?:`|\")?(\w+)(?:`|\")?\s+([\w()]+)",
            re.IGNORECASE | re.MULTILINE,
        )
        skip_keywords = {
            "primary", "unique", "check", "constraint", "foreign", "index", "key",
            "exclude", "references",
        }

        for table_match in table_pattern.finditer(content):
            table_name = table_match.group(1).lower()
            body = table_match.group(2)
            columns = []
            for col_match in col_pattern.finditer(body):
                col_name = col_match.group(1).lower()
                col_type = col_match.group(2).upper()
                if col_name in skip_keywords:
                    continue
                columns.append({"name": col_name, "type": col_type})
            if columns:
                result[table_name] = columns
    except Exception:
        pass
    return result


# ==============================================================================
# DynamoDB schema-file parser
#
# Reads knowledge/dynamodb-schema.yaml into a dict keyed by table name.
# Format (all fields except `name` optional; a bare {name: X} entry falls back to
# an existence-only check, matching the pre-enhancement behavior):
#
#   tables:
#     - name: customers
#       partition_key: {name: customer_id, type: S}
#       sort_key: {name: created_at, type: N}          # or null / omit
#       global_secondary_indexes:
#         - name: email-index
#           partition_key: {name: email, type: S}
#           sort_key: null
#       required_attributes: [customer_id, email, signup_date]
#       min_row_count: 1
#       sample_items: false     # opt-in 25-item Scan for attribute drift check
# ==============================================================================

DYNAMODB_SCHEMA_FILENAME = "dynamodb-schema.yaml"


def _extract_dynamodb_schemas(knowledge_dir: Path) -> dict[str, dict]:
    """
    Load per-table DynamoDB schema expectations from
    knowledge/dynamodb-schema.yaml.

    Returns {} if the file doesn't exist or has no `tables` list. Missing
    optional fields default to None / [] so downstream code can treat each
    table entry uniformly.
    """
    if not knowledge_dir.exists():
        return {}
    candidate = knowledge_dir / DYNAMODB_SCHEMA_FILENAME
    if not candidate.exists():
        return {}
    try:
        raw = read_yaml(candidate) or {}
    except Exception:
        return {}
    tables = raw.get("tables") or []
    schemas: dict[str, dict] = {}
    for entry in tables:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        schemas[name] = {
            "name": name,
            "partition_key": entry.get("partition_key"),
            "sort_key": entry.get("sort_key"),
            "global_secondary_indexes": entry.get("global_secondary_indexes") or [],
            "required_attributes": entry.get("required_attributes") or [],
            "min_row_count": int(entry.get("min_row_count") or 0),
            "sample_items": bool(entry.get("sample_items", False)),
        }
    return schemas


def _dynamo_key_from_describe(desc: dict, index_key_schema: list | None = None) -> tuple[dict | None, dict | None]:
    """
    Extract (partition_key, sort_key) as {name, type} dicts from a DescribeTable
    payload or a GSI's KeySchema. Returns (None, None) if the key info is absent.
    """
    if index_key_schema is not None:
        key_schema = index_key_schema
        attr_defs = desc.get("AttributeDefinitions") or []
    else:
        table = desc.get("Table") or desc
        key_schema = table.get("KeySchema") or []
        attr_defs = table.get("AttributeDefinitions") or []
    attr_type = {a["AttributeName"]: a["AttributeType"] for a in attr_defs if "AttributeName" in a}
    pk = sk = None
    for k in key_schema:
        entry = {"name": k.get("AttributeName"), "type": attr_type.get(k.get("AttributeName"))}
        if k.get("KeyType") == "HASH":
            pk = entry
        elif k.get("KeyType") == "RANGE":
            sk = entry
    return pk, sk


def _dynamo_keys_match(expected: dict | None, actual: dict | None) -> bool:
    """Compare two {name, type} key dicts case-insensitively. None matches None."""
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    exp_name = (expected.get("name") or "").strip()
    exp_type = (expected.get("type") or "").strip().upper()
    act_name = (actual.get("name") or "").strip()
    act_type = (actual.get("type") or "").strip().upper()
    return exp_name == act_name and exp_type == act_type


# ==============================================================================
# Type matching
# ==============================================================================

def _type_matches(expected: str, actual: str) -> bool:
    """Case-insensitive prefix match for SQL types with common alias support."""
    e = expected.upper().split("(")[0].strip()
    a = actual.upper().split("(")[0].strip()
    aliases = {
        "INT": {"INT4", "INTEGER", "INT"},
        "INTEGER": {"INT4", "INTEGER", "INT"},
        "BIGINT": {"INT8", "BIGINT"},
        "SMALLINT": {"INT2", "SMALLINT"},
        "VARCHAR": {"VARCHAR", "CHARACTER VARYING", "TEXT"},
        "TEXT": {"TEXT", "VARCHAR", "CHARACTER VARYING"},
        "BOOL": {"BOOL", "BOOLEAN"},
        "BOOLEAN": {"BOOL", "BOOLEAN"},
        "TIMESTAMP": {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP WITH TIME ZONE"},
        "TIMESTAMPTZ": {"TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP"},
        "FLOAT": {"FLOAT4", "FLOAT8", "REAL", "DOUBLE PRECISION", "FLOAT"},
        "DOUBLE": {"FLOAT8", "DOUBLE PRECISION"},
        "UUID": {"UUID"},
        "JSON": {"JSON", "JSONB"},
        "JSONB": {"JSON", "JSONB"},
        "SERIAL": {"INT4", "INTEGER", "SERIAL"},
        "BIGSERIAL": {"INT8", "BIGINT", "BIGSERIAL"},
    }
    if e in aliases:
        return a in aliases[e]
    return e == a


def _build_error_summary(missing_tables: list, missing_columns: list) -> str:
    parts = []
    if missing_tables:
        parts.append(f"Missing tables: {', '.join(missing_tables)}")
    if missing_columns:
        col_desc = [f"{c['table']}.{c['column']}" for c in missing_columns[:5]]
        parts.append(f"Missing columns: {', '.join(col_desc)}")
    return "; ".join(parts)


# ==============================================================================
# Data readiness checks  -- Database
# ==============================================================================

def _fetch_password_from_gcp_secret(secret_path: str) -> tuple[str | None, str | None]:
    """Fetch password from GCP Secret Manager. Returns (password, error)."""
    try:
        from google.cloud import secretmanager
    except ImportError:
        return None, "google-cloud-secret-manager not installed"
    try:
        sm_client = secretmanager.SecretManagerServiceClient()
        resp = sm_client.access_secret_version(request={"name": secret_path})
        payload = resp.payload.data.decode("utf-8")
        try:
            blob = json.loads(payload)
            if isinstance(blob, dict):
                return (blob.get("password") or blob.get("PASSWORD") or payload), None
            return payload, None
        except Exception:
            return payload, None
    except Exception as e:
        return None, f"Secret Manager fetch failed: {e}"


def _quote_identifier(value: str) -> str:
    """Quote a PostgreSQL identifier for drivers without sql.Identifier (pg8000)."""
    return '"' + str(value).replace('"', '""') + '"'


def _run_postgres_validation(cur, schema: str, expected_tables: list[str],
                              expected_columns: dict, min_row_counts: dict,
                              paramstyle: str = "pyformat") -> dict:
    """
    Run the read-only validation SQL given an open DBAPI cursor.
    Works with psycopg2 (paramstyle='pyformat') and pg8000 (paramstyle='format').
    """
    ph = "%s" if paramstyle in ("pyformat", "format") else "?"

    cur.execute(
        f"SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = {ph} AND table_type = 'BASE TABLE'",
        (schema,)
    )
    existing_tables = {row[0].lower() for row in cur.fetchall()}
    expected_lower = [t.lower() for t in expected_tables]
    found = [t for t in expected_lower if t in existing_tables]
    missing = [t for t in expected_lower if t not in existing_tables]
    unexpected = [t for t in existing_tables if t not in expected_lower]

    schema_mismatches = []
    columns_missing = []
    actual_columns = {}

    for table in found:
        cur.execute(
            f"SELECT column_name, data_type, udt_name, is_nullable, column_default "
            f"FROM information_schema.columns "
            f"WHERE table_schema = {ph} AND table_name = {ph} "
            f"ORDER BY ordinal_position",
            (schema, table)
        )
        rows = cur.fetchall()
        actual_cols = []
        for col_name, data_type, udt_name, nullable, default in rows:
            actual_cols.append({
                "name": col_name.lower(),
                "type": udt_name.upper() if udt_name else data_type.upper(),
                "nullable": nullable == "YES",
                "default": default,
            })
        actual_columns[table] = actual_cols

        if table in expected_columns:
            actual_by_name = {c["name"]: c["type"] for c in actual_cols}
            for exp_col in expected_columns[table]:
                exp_name = exp_col["name"].lower()
                exp_type = exp_col["type"].upper()
                if exp_name not in actual_by_name:
                    columns_missing.append({
                        "table": table, "column": exp_name,
                        "expected_type": exp_type, "actual_type": None,
                    })
                else:
                    actual_type = actual_by_name[exp_name]
                    if not _type_matches(exp_type, actual_type):
                        schema_mismatches.append({
                            "table": table, "column": exp_name,
                            "expected_type": exp_type, "actual_type": actual_type,
                        })

    row_counts = {}
    tables_with_data = []
    tables_empty = []
    row_count_failures = []

    row_count_sql = "SELECT count(*) FROM (SELECT 1 FROM {}.{} LIMIT 100001) sub"

    for table in found:
        try:
            if paramstyle == "pyformat":
                # psycopg2 — let the driver compose the identifiers.
                from psycopg2 import sql

                cur.execute(
                    sql.SQL(row_count_sql).format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
            else:
                # pg8000 (Cloud SQL) has no sql.Identifier and rejects a Composed
                # object, so quote the identifiers with the PostgreSQL escape rule:
                # double any embedded quote.
                cur.execute(
                    row_count_sql.format(
                        _quote_identifier(schema), _quote_identifier(table)
                    )
                )
            count = cur.fetchone()[0]
            row_counts[table] = count if count <= 100000 else "100000+"
            if count > 0:
                tables_with_data.append(table)
            else:
                tables_empty.append(table)
                if table in min_row_counts and min_row_counts[table] > 0:
                    row_count_failures.append({
                        "table": table,
                        "expected_min": min_row_counts[table],
                        "actual": 0,
                    })
        except Exception:
            row_counts[table] = "error"
            tables_empty.append(table)

    critical_failures = len(missing) > 0 or len(columns_missing) > 0
    passed = not critical_failures

    return {
        "passed": passed,
        "error": None if passed else _build_error_summary(missing, columns_missing),
        "tables_found": found,
        "tables_missing": missing,
        "tables_unexpected": unexpected,
        "tables_with_data": tables_with_data,
        "tables_empty": tables_empty,
        "row_counts": row_counts,
        "row_count_failures": row_count_failures,
        "columns_missing": columns_missing,
        "schema_mismatches": schema_mismatches,
        "actual_columns": actual_columns,
        "total_tables_in_schema": len(existing_tables),
    }


def check_postgres_tables(config: dict, expected_tables: list[str],
                          expected_columns: dict | None = None,
                          min_row_counts: dict | None = None) -> dict:
    """
    Validate PostgreSQL schema and data presence. Read-only.

    Connection mode:
      - If config has 'connection_name' (Cloud SQL), use Cloud SQL Python Connector
        with pg8000 driver. Auth via 'iam' or 'secret-manager' (matches cloud-readiness).
      - Else fall back to psycopg2 with hostname/port (RDS, Azure, generic).
    """
    if expected_columns is None:
        expected_columns = {}
    if min_row_counts is None:
        min_row_counts = {}

    schema = config.get("schema", "public")
    start = time.perf_counter()

    connection_name = config.get("connection_name", "")
    if connection_name:
        return _check_cloud_sql_postgres(
            config, schema, expected_tables, expected_columns, min_row_counts, start
        )
    return _check_generic_postgres(
        config, schema, expected_tables, expected_columns, min_row_counts, start
    )


def _check_cloud_sql_postgres(config, schema, expected_tables, expected_columns,
                               min_row_counts, start) -> dict:
    """Cloud SQL connection path -- uses google.cloud.sql.connector + pg8000."""
    connection_name = config["connection_name"]
    database = config.get("database", "postgres")
    username = config.get("username", "postgres")
    auth_method = config.get("auth_method", "iam")
    secret_path = config.get("secret_path", "")

    password = None
    if auth_method == "secret-manager":
        if not secret_path:
            return {
                "passed": False, "latency_ms": 0,
                "error": "auth_method=secret-manager requires secret_path",
                "tables_found": [], "tables_missing": expected_tables,
            }
        password, err = _fetch_password_from_gcp_secret(secret_path)
        if err:
            return {
                "passed": False, "latency_ms": 0, "error": err,
                "tables_found": [], "tables_missing": expected_tables,
            }

    try:
        from google.cloud.sql.connector import Connector
        import pg8000  # noqa: F401
    except ImportError as e:
        return {
            "passed": False, "latency_ms": 0,
            "error": (
                "cloud-sql-python-connector not installed. "
                "Install: pip install cloud-sql-python-connector[pg8000]. "
                f"Detail: {e}"
            ),
            "tables_found": [], "tables_missing": expected_tables,
        }

    ident = gcp_identity()
    print_identity_banner(ident)
    connector = None
    conn = None
    try:
        connector = Connector()
        connect_kwargs = {"user": username, "db": database}
        if auth_method == "secret-manager":
            connect_kwargs["password"] = password
            connect_kwargs["enable_iam_auth"] = False
        else:
            connect_kwargs["enable_iam_auth"] = True

        conn = connector.connect(connection_name, "pg8000", **connect_kwargs)
        cur = conn.cursor()
        result = _run_postgres_validation(
            cur, schema, expected_tables, expected_columns, min_row_counts,
            paramstyle="format",
        )
        cur.close()
        elapsed = (time.perf_counter() - start) * 1000
        result["latency_ms"] = round(elapsed, 1)
        result["probe_mode"] = (
            "iam-auth" if auth_method != "secret-manager"
            else "password-via-secret-manager"
        )
        return result
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False, "latency_ms": round(elapsed, 1),
            "error": str(e), "tables_found": [], "tables_missing": expected_tables,
        }
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            if connector is not None:
                connector.close()
        except Exception:
            pass


def _check_generic_postgres(config, schema, expected_tables, expected_columns,
                             min_row_counts, start) -> dict:
    """Generic Postgres path -- uses psycopg2 with hostname/port.

    Supports auth_method='secret-manager' / 'full-config-from-secret' by
    fetching credentials from AWS Secrets Manager. For full-config, the
    secret payload supplies endpoint/port/dbname/username/password directly
    (accepts either strict AWS keys `host`/`dbname` or the cloudless-deploy
    synonyms `endpoint`/`database`). For secret-manager, only the password
    is pulled from the secret; endpoint/db/user come from config.
    """
    try:
        import psycopg2
    except ImportError:
        return {
            "passed": False, "latency_ms": 0,
            "error": "psycopg2 not installed  -- pip install psycopg2-binary",
            "tables_found": [], "tables_missing": expected_tables,
        }

    auth_method = (config.get("auth_method") or "").lower()
    secret_arn_or_name = config.get("secret_arn") or config.get("secret_name") or config.get("secret_path")

    endpoint = config.get("endpoint") or config.get("hostname", "")
    database = config.get("database")
    username = config.get("username")
    password = config.get("password")

    # Fetch credentials from AWS Secrets Manager if requested.
    if auth_method in ("secret-manager", "full-config-from-secret"):
        if not secret_arn_or_name:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"auth_method={auth_method} requires secret_arn / secret_name / secret_path in config",
                "tables_found": [], "tables_missing": expected_tables,
            }
        try:
            import boto3
            import json as _json
            aws_profile = config.get("aws_profile") or None
            aws_region = config.get("region") or None
            session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
            sm = session.client("secretsmanager")
            secret_str = sm.get_secret_value(SecretId=secret_arn_or_name).get("SecretString")
            if not secret_str:
                return {
                    "passed": False, "latency_ms": 0,
                    "error": "SecretMalformed: SecretString missing (binary secrets not supported)",
                    "tables_found": [], "tables_missing": expected_tables,
                }
            payload = _json.loads(secret_str)
            def _pick(*keys):
                for k in keys:
                    v = payload.get(k)
                    if v not in (None, ""):
                        return v
                return None
            if auth_method == "full-config-from-secret":
                endpoint = endpoint or _pick("host", "endpoint")
                port_from_secret = _pick("port")
                if port_from_secret is not None:
                    config = dict(config)  # avoid mutating caller
                    config["port"] = int(port_from_secret)
                database = database or _pick("dbname", "database")
                username = username or _pick("username")
            password = _pick("password")
        except ImportError as e:
            return {
                "passed": False, "latency_ms": 0,
                "error": f"boto3 not installed -- pip install boto3. Detail: {e}",
                "tables_found": [], "tables_missing": expected_tables,
            }
        except Exception as e:
            err_str = str(e)
            if "ResourceNotFoundException" in err_str or "not found" in err_str.lower():
                return {
                    "passed": False, "latency_ms": 0,
                    "error": f"SecretNotFound: {secret_arn_or_name!r}",
                    "tables_found": [], "tables_missing": expected_tables,
                }
            if "AccessDeniedException" in err_str or "not authorized" in err_str.lower():
                return {
                    "passed": False, "latency_ms": 0,
                    "error": f"AccessDenied: caller cannot read secret (needs secretsmanager:GetSecretValue)",
                    "tables_found": [], "tables_missing": expected_tables,
                }
            return {
                "passed": False, "latency_ms": 0,
                "error": f"Secrets Manager fetch failed: {err_str}",
                "tables_found": [], "tables_missing": expected_tables,
            }

    missing = []
    if not endpoint:
        missing.append("endpoint/hostname")
    if not database:
        missing.append("database")
    if not username:
        missing.append("username")
    if missing:
        return {
            "passed": False, "latency_ms": 0,
            "error": (
                f"Postgres check requires {', '.join(missing)} in config "
                f"(refusing to silently default to postgres/postgres)"
            ),
            "tables_found": [], "tables_missing": expected_tables,
        }

    connection_params = {}
    if ":" in endpoint:
        host, port_str = endpoint.rsplit(":", 1)
        connection_params["host"] = host
        connection_params["port"] = int(port_str)
    else:
        connection_params["host"] = endpoint
        connection_params["port"] = config.get("port", 5432)

    connection_params["dbname"] = database
    connection_params["user"] = username
    # Prefer secret-fetched password over inline config password
    if password:
        connection_params["password"] = password
    elif config.get("password"):
        connection_params["password"] = config["password"]
    connection_params["connect_timeout"] = int(config.get("connect_timeout_s", 10))
    statement_timeout_ms = int(config.get("statement_timeout_ms", 10000))
    connection_params["options"] = f"-c statement_timeout={statement_timeout_ms}"

    try:
        conn = psycopg2.connect(**connection_params)
        conn.set_session(readonly=True)
        cur = conn.cursor()
        result = _run_postgres_validation(
            cur, schema, expected_tables, expected_columns, min_row_counts,
            paramstyle="pyformat",
        )
        cur.close()
        conn.close()
        elapsed = (time.perf_counter() - start) * 1000
        result["latency_ms"] = round(elapsed, 1)
        result["probe_mode"] = "psycopg2-hostname"
        return result
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": False, "latency_ms": round(elapsed, 1),
            "error": str(e), "tables_found": [], "tables_missing": expected_tables,
        }


def check_dynamodb_tables(
    config: dict,
    expected_tables: list[str],
    expected_schemas: dict[str, dict] | None = None,
) -> dict:
    """
    Check that expected DynamoDB tables exist and (when expected_schemas is
    provided) that their key schema, GSIs, and optional item shape match.

    Backward-compatible: callers that pass only (config, expected_tables) get the
    original existence + ItemCount behavior. When expected_schemas is provided
    with per-table shape info (partition_key/sort_key/global_secondary_indexes/
    required_attributes/min_row_count/sample_items), the handler additionally:

      - Compares live KeySchema against expected (critical failure on mismatch).
      - Compares GSIs by name + key schema (deviation only).
      - When sample_items=true, Scans up to 25 items and reports which required
        attributes were missing on any sampled item (deviation only).
      - Fails the gate if a table's live ItemCount is below min_row_count.
    """
    start = time.perf_counter()
    try:
        import boto3
    except ImportError:
        return {"passed": False, "latency_ms": 0, "error": "boto3 not installed"}

    profile = config.get("aws_profile")
    region, region_source = discover_aws_region(config, profile=profile)
    if not region:
        return {
            "passed": False, "latency_ms": 0,
            "error": (
                "DynamoDB region not found. Tried: config.region, env (AWS_REGION/AWS_DEFAULT_REGION), "
                f"`aws configure get region` (profile={profile or 'default'}). "
                "Set one of these or pass --region."
            ),
        }
    ident = aws_identity(profile=profile)
    print_identity_banner(ident)
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        client = session.client("dynamodb", region_name=region)

        existing = set()
        paginator = client.get_paginator("list_tables")
        for page in paginator.paginate():
            existing.update(page.get("TableNames", []))

        found = [t for t in expected_tables if t in existing]
        missing = [t for t in expected_tables if t not in existing]

        schemas = expected_schemas or {}

        row_counts: dict[str, int | str] = {}
        row_count_failures: list[dict] = []
        key_schemas: dict[str, dict] = {}
        key_schema_mismatches: list[dict] = []
        gsi_status: dict[str, dict] = {}
        gsi_mismatches: list[dict] = []
        gsi_missing: list[dict] = []
        attr_drift: list[dict] = []

        for table in found:
            try:
                desc = client.describe_table(TableName=table)
            except Exception as e:
                row_counts[table] = "error"
                key_schemas[table] = {"error": str(e)[:200]}
                continue

            table_desc = desc.get("Table") or {}
            item_count = table_desc.get("ItemCount", 0)
            row_counts[table] = item_count

            # 1. Live key-schema extraction. `partition_key` / `sort_key` are
            # extracted for convenience; `raw_describe_table` is the full AWS
            # DescribeTable payload so downstream consumers can inspect any
            # field (TableSize, StreamSpecification, SSEDescription, TTL, etc.)
            # without the handler having to know about it up front.
            live_pk, live_sk = _dynamo_key_from_describe(desc)
            key_schemas[table] = {
                "partition_key": live_pk,
                "sort_key": live_sk,
                "status": table_desc.get("TableStatus"),
                "raw_describe_table": table_desc,
            }

            live_gsis = {
                (gsi.get("IndexName") or ""): gsi
                for gsi in (table_desc.get("GlobalSecondaryIndexes") or [])
            }
            gsi_status[table] = {
                "live_gsi_names": sorted(live_gsis.keys()),
                "live_gsi_count": len(live_gsis),
            }

            schema = schemas.get(table)
            if not schema:
                continue

            # 2. Key-schema comparison (critical if declared)
            exp_pk = schema.get("partition_key")
            exp_sk = schema.get("sort_key")
            if exp_pk and not _dynamo_keys_match(exp_pk, live_pk):
                key_schema_mismatches.append({
                    "table": table, "field": "partition_key",
                    "expected": exp_pk, "actual": live_pk,
                })
            if exp_sk is not None and not _dynamo_keys_match(exp_sk, live_sk):
                key_schema_mismatches.append({
                    "table": table, "field": "sort_key",
                    "expected": exp_sk, "actual": live_sk,
                })

            # 3. GSI comparison (deviation only)
            for exp_gsi in schema.get("global_secondary_indexes") or []:
                gsi_name = (exp_gsi.get("name") or "").strip()
                if not gsi_name:
                    continue
                live_gsi = live_gsis.get(gsi_name)
                if live_gsi is None:
                    gsi_missing.append({"table": table, "gsi": gsi_name})
                    continue
                exp_gsi_pk = exp_gsi.get("partition_key")
                exp_gsi_sk = exp_gsi.get("sort_key")
                live_gsi_pk, live_gsi_sk = _dynamo_key_from_describe(
                    table_desc, index_key_schema=live_gsi.get("KeySchema") or [],
                )
                if exp_gsi_pk and not _dynamo_keys_match(exp_gsi_pk, live_gsi_pk):
                    gsi_mismatches.append({
                        "table": table, "gsi": gsi_name, "field": "partition_key",
                        "expected": exp_gsi_pk, "actual": live_gsi_pk,
                    })
                if exp_gsi_sk is not None and not _dynamo_keys_match(exp_gsi_sk, live_gsi_sk):
                    gsi_mismatches.append({
                        "table": table, "gsi": gsi_name, "field": "sort_key",
                        "expected": exp_gsi_sk, "actual": live_gsi_sk,
                    })

            # 4. Min-row-count check (critical)
            min_rows = int(schema.get("min_row_count") or 0)
            if min_rows > 0 and isinstance(item_count, int) and item_count < min_rows:
                row_count_failures.append({
                    "table": table, "expected_min": min_rows, "actual": item_count,
                })

            # 5. Optional item sampling for attribute drift (deviation)
            if schema.get("sample_items") and schema.get("required_attributes"):
                try:
                    scan = client.scan(TableName=table, Limit=25)
                    items = scan.get("Items") or []
                    if items:
                        for req_attr in schema["required_attributes"]:
                            missing_on = sum(1 for it in items if req_attr not in it)
                            if missing_on:
                                attr_drift.append({
                                    "table": table,
                                    "required_attribute": req_attr,
                                    "sampled": len(items),
                                    "missing_on_sampled": missing_on,
                                })
                except Exception as e:
                    attr_drift.append({
                        "table": table, "error": f"Sample scan failed: {str(e)[:200]}",
                    })

        critical_failures = (
            len(missing) > 0
            or len(key_schema_mismatches) > 0
            or len(row_count_failures) > 0
        )
        elapsed = (time.perf_counter() - start) * 1000

        error_bits = []
        if missing:
            error_bits.append(f"Missing tables: {', '.join(missing)}")
        if key_schema_mismatches:
            error_bits.append(f"Key schema mismatches on {len(key_schema_mismatches)} field(s)")
        if row_count_failures:
            error_bits.append(f"Row-count minimums not met on {len(row_count_failures)} table(s)")

        return {
            "passed": not critical_failures,
            "latency_ms": round(elapsed, 1),
            "error": "; ".join(error_bits) if error_bits else None,
            "tables_found": found,
            "tables_missing": missing,
            "row_counts": row_counts,
            "row_count_failures": row_count_failures,
            "key_schemas": key_schemas,
            "key_schema_mismatches": key_schema_mismatches,
            "gsi_status": gsi_status,
            "gsi_mismatches": gsi_mismatches,
            "gsi_missing": gsi_missing,
            "attr_drift": attr_drift,
            "schema_source": "dynamodb-schema.yaml" if expected_schemas else "existence-only",
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


# ==============================================================================
# Data readiness checks  -- Storage
# ==============================================================================

def check_s3_files(config: dict, expected_prefixes: list[dict]) -> dict:
    """
    Check S3 bucket for expected file prefixes/types.
    expected_prefixes: [{prefix: "templates/", min_count: 1, file_types: [".json"]}]
    """
    start = time.perf_counter()
    try:
        import boto3
    except ImportError:
        return {"passed": False, "latency_ms": 0, "error": "boto3 not installed"}

    bucket_name = config.get("bucket_name", "")
    if not bucket_name:
        return {"passed": False, "latency_ms": 0, "error": "No bucket_name provided"}

    profile = config.get("aws_profile")
    region, region_source = discover_aws_region(config, profile=profile)
    if not region:
        return {
            "passed": False, "latency_ms": 0,
            "error": (
                "S3 region not found. Tried: config.region, env (AWS_REGION/AWS_DEFAULT_REGION), "
                f"`aws configure get region` (profile={profile or 'default'}). "
                "Set one of these or pass --region."
            ),
        }
    ident = aws_identity(profile=profile)
    print_identity_banner(ident)
    try:
        session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        client = session.client("s3", region_name=region)

        results = []
        all_passed = True

        for expectation in expected_prefixes:
            prefix = expectation.get("prefix", "")
            min_count = expectation.get("min_count", 1)
            file_types = expectation.get("file_types", [])

            resp = client.list_objects_v2(
                Bucket=bucket_name, Prefix=prefix, MaxKeys=1000
            )
            objects = resp.get("Contents", [])
            truncated = bool(resp.get("IsTruncated", False))

            if file_types:
                objects = [
                    o for o in objects
                    if any(o["Key"].lower().endswith(ft.lower()) for ft in file_types)
                ]

            count = len(objects)
            check_passed = count >= min_count
            if not check_passed:
                all_passed = False

            results.append({
                "prefix": prefix,
                "expected_min": min_count,
                "actual_count": count,
                "file_types_filter": file_types,
                "passed": check_passed,
                "sample_keys": [o["Key"] for o in objects[:3]],
                "truncated": truncated,
                "note": "More than 1000 objects under prefix — partial scan" if truncated else None,
            })

        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": all_passed,
            "latency_ms": round(elapsed, 1),
            "error": None if all_passed else "Some prefix checks failed",
            "prefix_checks": results,
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def check_gcs_files(config: dict, expected_prefixes: list[dict]) -> dict:
    """Check GCS bucket for expected file prefixes/types."""
    start = time.perf_counter()
    try:
        from google.cloud import storage as gcs_storage
    except ImportError:
        return {"passed": False, "latency_ms": 0, "error": "google-cloud-storage not installed"}

    bucket_name = config.get("bucket_name", "")
    if not bucket_name:
        return {"passed": False, "latency_ms": 0, "error": "No bucket_name provided"}

    project_id, project_source = discover_gcp_project(config)
    ident = gcp_identity()
    print_identity_banner(ident)
    try:
        client = gcs_storage.Client(project=project_id) if project_id else gcs_storage.Client()
        bucket = client.bucket(bucket_name)

        results = []
        all_passed = True

        for expectation in expected_prefixes:
            prefix = expectation.get("prefix", "")
            min_count = expectation.get("min_count", 1)
            file_types = expectation.get("file_types", [])

            scan_cap = 1000
            blobs_iter = bucket.list_blobs(prefix=prefix, max_results=scan_cap + 1)
            blobs = list(blobs_iter)
            truncated = len(blobs) > scan_cap
            if truncated:
                blobs = blobs[:scan_cap]
            if file_types:
                blobs = [
                    b for b in blobs
                    if any(b.name.lower().endswith(ft.lower()) for ft in file_types)
                ]

            count = len(blobs)
            check_passed = count >= min_count
            if not check_passed:
                all_passed = False

            results.append({
                "prefix": prefix,
                "expected_min": min_count,
                "actual_count": count,
                "file_types_filter": file_types,
                "passed": check_passed,
                "sample_keys": [b.name for b in blobs[:3]],
                "truncated": truncated,
                "note": "More than 1000 objects under prefix — partial scan" if truncated else None,
            })

        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": all_passed,
            "latency_ms": round(elapsed, 1),
            "error": None if all_passed else "Some prefix checks failed",
            "prefix_checks": results,
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


def check_azure_blob_files(config: dict, expected_prefixes: list[dict]) -> dict:
    """Check Azure Blob Storage for expected file prefixes/types."""
    start = time.perf_counter()
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return {"passed": False, "latency_ms": 0, "error": "azure-storage-blob not installed"}

    account_url = config.get("account_url", "")
    container_name = config.get("container_name", "")
    if not account_url or not container_name:
        return {"passed": False, "latency_ms": 0, "error": "account_url and container_name required"}

    ident = azure_identity()
    print_identity_banner(ident)
    try:
        credential = DefaultAzureCredential()
        blob_service = BlobServiceClient(account_url=account_url, credential=credential)
        container_client = blob_service.get_container_client(container_name)

        results = []
        all_passed = True

        for expectation in expected_prefixes:
            prefix = expectation.get("prefix", "")
            min_count = expectation.get("min_count", 1)
            file_types = expectation.get("file_types", [])

            scan_cap = 1000
            blobs_iter = container_client.list_blobs(name_starts_with=prefix)
            blobs = []
            truncated = False
            for b in blobs_iter:
                if len(blobs) >= scan_cap:
                    truncated = True
                    break
                blobs.append(b)
            if file_types:
                blobs = [
                    b for b in blobs
                    if any(b.name.lower().endswith(ft.lower()) for ft in file_types)
                ]

            count = len(blobs)
            check_passed = count >= min_count
            if not check_passed:
                all_passed = False

            results.append({
                "prefix": prefix,
                "expected_min": min_count,
                "actual_count": count,
                "file_types_filter": file_types,
                "passed": check_passed,
                "sample_keys": [b.name for b in blobs[:3]],
                "truncated": truncated,
                "note": "More than 1000 objects under prefix — partial scan" if truncated else None,
            })

        elapsed = (time.perf_counter() - start) * 1000
        return {
            "passed": all_passed,
            "latency_ms": round(elapsed, 1),
            "error": None if all_passed else "Some prefix checks failed",
            "prefix_checks": results,
        }
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return {"passed": False, "latency_ms": round(elapsed, 1), "error": str(e)}


# ==============================================================================
# Data readiness checks  -- LangSmith (SaaS API)
# ==============================================================================

def _langsmith_resolve_api_key(config: dict) -> str | None:
    """
    Resolve a LangSmith API key without writing it to disk.
    Order: GCP Secret Manager (if auth_method=secret-manager + provider=gcp)
           -> env LANGSMITH_API_KEY -> env LANGCHAIN_API_KEY
    Mirrors the pattern from cloud/handlers/langsmith.py — the gate
    doesn't pre-fetch credentials for SaaS integrations the way it does
    for cloud-native services, so the handler self-fetches.
    """
    import os
    auth_method = (config.get("auth_method") or "").lower()
    provider = (config.get("provider") or "").lower()
    secret_path = config.get("secret_path") or ""
    if auth_method == "secret-manager" and provider == "gcp" and secret_path:
        try:
            from google.cloud import secretmanager  # type: ignore
            client = secretmanager.SecretManagerServiceClient()
            resp = client.access_secret_version(request={"name": secret_path})
            payload = resp.payload.data.decode("utf-8").strip()
            try:
                blob = json.loads(payload)
                if isinstance(blob, dict):
                    return (
                        blob.get("api_key")
                        or blob.get("LANGSMITH_API_KEY")
                        or blob.get("LANGCHAIN_API_KEY")
                        or blob.get("password")
                        or payload
                    )
            except Exception:
                return payload
        except Exception:
            pass
    return os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")


def check_langsmith_datasets(
    config: dict,
    expected_ids: list[str] | None = None,
    expected_names: list[str] | None = None,
    min_example_counts: dict | None = None,
    default_min: int = 1,
    expected_project_ids: list[str] | None = None,
    expected_project_names: list[str] | None = None,
) -> dict:
    """
    Verify LangSmith datasets exist (by ID or by name) and contain at least
    `default_min` examples (or per-dataset overrides via min_example_counts).
    Optionally verifies LangSmith projects (sessions) exist by ID or name.
    Read-only: only GETs against /api/v1/datasets, /api/v1/examples, /api/v1/sessions.

    Hard-fails (passed=False) when ANY listed resource is missing or below
    its minimum example threshold.
    """
    import time
    from urllib.parse import quote
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError

    expected_ids = expected_ids or []
    expected_names = expected_names or []
    min_example_counts = min_example_counts or {}
    expected_project_ids = expected_project_ids or []
    expected_project_names = expected_project_names or []

    has_dataset_check = bool(expected_ids or expected_names)
    has_project_check = bool(expected_project_ids or expected_project_names)

    if not has_dataset_check and not has_project_check:
        return {
            "passed": True, "latency_ms": 0,
            "note": "No dataset_ids/names or project_ids/names supplied — skipping LangSmith data check.",
        }

    api_url = (config.get("api_url") or "").rstrip("/")
    if not api_url:
        return {
            "passed": False, "latency_ms": 0,
            "error": "Missing config.api_url for LangSmith data-readiness check",
        }

    api_key = _langsmith_resolve_api_key(config)
    if not api_key:
        return {
            "passed": False, "latency_ms": 0,
            "error": (
                "No LangSmith API key resolvable (tried secret_path + env vars). "
                "Cannot probe dataset content without authentication."
            ),
        }

    headers = {
        "x-api-key": api_key,
        "User-Agent": "aah-data-readiness/1.0",
    }
    start = time.perf_counter()

    def _get_json(path: str):
        url = f"{api_url}{path}"
        try:
            resp = urlopen(Request(url, headers=headers, method="GET"), timeout=15)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.getcode(), json.loads(body), None
            except ValueError:
                return resp.getcode(), None, "non-JSON response"
        except HTTPError as e:
            return e.code, None, f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            return 0, None, f"Network error: {e.reason}"
        except Exception as e:
            return 0, None, str(e)

    # Resolve names -> IDs first, then merge with explicit IDs.
    resolved_targets = []  # list of {id, name}
    name_failures = []
    for name in expected_names:
        n = str(name).strip()
        if not n:
            continue
        status, body, err = _get_json(f"/api/v1/datasets?name={quote(n)}&limit=10")
        if err:
            name_failures.append({"name": n, "error": err})
            continue
        items = body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])
        match = next(
            (item for item in items if isinstance(item, dict) and str(item.get("name", "")).lower() == n.lower()),
            None,
        )
        if not match:
            name_failures.append({"name": n, "error": "not found"})
            continue
        resolved_targets.append({"id": match.get("id"), "name": match.get("name"), "source": "name"})

    id_failures = []
    for did in expected_ids:
        rid = str(did).strip()
        if not rid:
            continue
        status, body, err = _get_json(f"/api/v1/datasets/{rid}")
        if err or not isinstance(body, dict) or not body.get("id"):
            id_failures.append({"id": rid, "error": err or f"HTTP {status} or empty body"})
            continue
        resolved_targets.append({"id": body.get("id"), "name": body.get("name"), "source": "id"})

    # Dedupe by id so name+id passing the same dataset doesn't double-probe
    seen = set()
    targets = []
    for t in resolved_targets:
        tid = str(t.get("id"))
        if tid and tid not in seen:
            seen.add(tid)
            targets.append(t)

    # Probe example counts. /api/v1/examples?dataset=<id>&limit=<N>.
    # We cap probe at min_required+1 — proves "at least N" without scanning huge sets.
    row_counts = {}
    row_count_failures = []
    for t in targets:
        tid = str(t["id"])
        required = int(min_example_counts.get(tid, default_min))
        probe_limit = max(1, required + 1)
        status, body, err = _get_json(f"/api/v1/examples?dataset={tid}&limit={probe_limit}")
        if err:
            row_counts[tid] = "error"
            row_count_failures.append({
                "dataset_id": tid, "dataset_name": t.get("name"),
                "expected_min": required, "actual": 0, "error": err,
            })
            continue
        items = body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])
        actual = len(items)
        # We capped at probe_limit, so we report ">= probe_limit" rather than the cap value
        recorded = actual if actual < probe_limit else f">={probe_limit}"
        row_counts[tid] = recorded
        if actual < required:
            row_count_failures.append({
                "dataset_id": tid, "dataset_name": t.get("name"),
                "expected_min": required, "actual": actual,
            })

    # ---- Project (session) resolution — same pattern as datasets ----
    project_targets = []
    project_name_failures = []
    project_id_failures = []
    if has_project_check:
        for name in expected_project_names:
            n = str(name).strip()
            if not n:
                continue
            status, body, err = _get_json(f"/api/v1/sessions?name={quote(n)}&limit=10")
            if err:
                project_name_failures.append({"name": n, "error": err})
                continue
            items = body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])
            match = next(
                (item for item in items if isinstance(item, dict) and str(item.get("name", "")).lower() == n.lower()),
                None,
            )
            if not match:
                project_name_failures.append({"name": n, "error": "not found"})
                continue
            project_targets.append({"id": match.get("id"), "name": match.get("name"), "source": "name"})

        for pid in expected_project_ids:
            rid = str(pid).strip()
            if not rid:
                continue
            status, body, err = _get_json(f"/api/v1/sessions/{rid}")
            if err or not isinstance(body, dict) or not body.get("id"):
                project_id_failures.append({"id": rid, "error": err or f"HTTP {status} or empty body"})
                continue
            project_targets.append({"id": body.get("id"), "name": body.get("name"), "source": "id"})

    elapsed = (time.perf_counter() - start) * 1000

    found_summary = [{"id": t["id"], "name": t.get("name")} for t in targets]
    project_found_summary = [{"id": t["id"], "name": t.get("name")} for t in project_targets]
    passed = (
        len(name_failures) == 0
        and len(id_failures) == 0
        and len(row_count_failures) == 0
        and len(project_name_failures) == 0
        and len(project_id_failures) == 0
    )

    error_parts = []
    if name_failures:
        error_parts.append(f"datasets missing by name: {[f['name'] for f in name_failures]}")
    if id_failures:
        error_parts.append(f"dataset IDs missing: {[f['id'] for f in id_failures]}")
    if row_count_failures:
        error_parts.append(
            f"row-count failures: " +
            ", ".join(f"{f['dataset_name'] or f['dataset_id']}={f['actual']}/{f['expected_min']}" for f in row_count_failures)
        )
    if project_name_failures:
        error_parts.append(f"projects missing by name: {[f['name'] for f in project_name_failures]}")
    if project_id_failures:
        error_parts.append(f"project IDs missing: {[f['id'] for f in project_id_failures]}")

    # Drop the local key reference once we're done.
    api_key = None

    return {
        "passed": passed,
        "latency_ms": round(elapsed, 1),
        "error": "; ".join(error_parts) if error_parts else None,
        "datasets_found": found_summary,
        "datasets_missing_by_name": name_failures,
        "datasets_missing_by_id": id_failures,
        "row_counts": row_counts,
        "row_count_failures": row_count_failures,
        "projects_found": project_found_summary,
        "projects_missing_by_name": project_name_failures,
        "projects_missing_by_id": project_id_failures,
    }


# ==============================================================================
# Knowledge-file loader for LangSmith expectations
# ==============================================================================

_LANGSMITH_KNOWLEDGE_FILES = (
    "langsmith-datasets.yaml",
    "langsmith-datasets.yml",
    "langsmith-datasets.json",
    "langsmith.yaml",
    "langsmith.yml",
    "langsmith.json",
)


def _load_langsmith_knowledge_file(project_path: Path) -> dict | None:
    """
    Search knowledge/ for any of the supported LangSmith config files.
    Returns a normalized dict with the schema:

      {
        "source_path": "knowledge/langsmith-datasets.yaml",
        "default_min_examples": 1,
        "datasets": [
          {"id": "<uuid>"|None, "name": "<name>"|None, "min_examples": int|None},
          ...
        ],
        "projects": [
          {"id": "<uuid>"|None, "name": "<name>"|None},
          ...
        ],
      }

    Returns None if no file is found. Returns a dict with empty lists if
    file is found but lists nothing — caller treats that as "knowledge mode
    active but nothing to validate".
    """
    try:
        from aah.core.knowledge.parser import find_knowledge_dir
        knowledge_dir = find_knowledge_dir(project_path)
    except Exception:
        knowledge_dir = None

    if not knowledge_dir or not knowledge_dir.is_dir():
        return None

    found_path = None
    for filename in _LANGSMITH_KNOWLEDGE_FILES:
        candidate = knowledge_dir / filename
        if candidate.is_file():
            found_path = candidate
            break
    if found_path is None:
        return None

    try:
        text = found_path.read_text(encoding="utf-8")
    except Exception:
        return None

    raw = None
    suffix = found_path.suffix.lower()
    try:
        if suffix == ".json":
            raw = json.loads(text)
        else:
            raw = read_yaml(found_path)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    default_min = int(raw.get("default_min_examples") or 1)

    datasets_raw = raw.get("datasets") or []
    datasets = []
    if isinstance(datasets_raw, list):
        for entry in datasets_raw:
            if not isinstance(entry, dict):
                continue
            ds = {
                "id": (entry.get("id") or None),
                "name": (entry.get("name") or None),
                "min_examples": entry.get("min_examples"),
            }
            if ds["id"] or ds["name"]:
                datasets.append(ds)

    projects_raw = raw.get("projects") or []
    projects = []
    if isinstance(projects_raw, list):
        for entry in projects_raw:
            if not isinstance(entry, dict):
                continue
            pj = {
                "id": (entry.get("id") or None),
                "name": (entry.get("name") or None),
            }
            if pj["id"] or pj["name"]:
                projects.append(pj)

    return {
        "source_path": str(found_path.relative_to(project_path)) if project_path in found_path.parents else str(found_path),
        "default_min_examples": default_min,
        "datasets": datasets,
        "projects": projects,
    }


# ==============================================================================
# Schema deviation comparison
# ==============================================================================

def compare_schema_deviations(
    expected_columns: dict[str, list[dict]],
    actual_result: dict,
) -> list[dict]:
    """
    Compare expected schema (from knowledge/ files) against actual DB state.
    Returns list of deviations for user presentation.
    """
    deviations = []

    for table in actual_result.get("tables_missing", []):
        deviations.append({
            "table": table, "issue": "Table missing from database",
            "expected": "exists (in schema file)", "actual": "(not found)",
        })

    for table in actual_result.get("tables_unexpected", []):
        row_count = actual_result.get("row_counts", {}).get(table, "?")
        deviations.append({
            "table": table, "issue": "Extra table in database",
            "expected": "(not in schema file)", "actual": f"exists, {row_count} rows",
        })

    for col in actual_result.get("columns_missing", []):
        deviations.append({
            "table": col["table"], "issue": f"Missing column: {col['column']}",
            "expected": f"{col['column']}: {col['expected_type']}", "actual": "(not found)",
        })

    for mm in actual_result.get("schema_mismatches", []):
        deviations.append({
            "table": mm["table"], "issue": f"Column type mismatch: {mm['column']}",
            "expected": f"{mm['column']}: {mm['expected_type']}",
            "actual": f"{mm['column']}: {mm['actual_type']}",
        })

    for rf in actual_result.get("row_count_failures", []):
        deviations.append({
            "table": rf["table"], "issue": "Empty reference table",
            "expected": f">={rf['expected_min']} rows", "actual": "0 rows",
        })

    return deviations


# ==============================================================================
# Use-case alignment
# ==============================================================================

def check_use_case_alignment(
    project_path: Path,
    confirmed_tables: list[str],
    confirmed_prefixes: list[dict],
    row_counts: dict,
) -> list[dict]:
    """
    Cross-reference confirmed data against domain_scope processes
    and integrations from the decision registry.
    Returns list of {process, matched_tables, matched_prefixes, status}.
    """
    registry_path = project_path / ".aah" / "discuss" / "decision-registry.yaml"
    if not registry_path.exists():
        return []

    registry = read_yaml(registry_path)
    context = registry.get("context", {})
    domain_scope = context.get("domain_scope", {})
    processes = domain_scope.get("processes", [])
    integrations = context.get("integrations", [])

    if not processes and not integrations:
        return []

    tables_lower = {t.lower() for t in confirmed_tables}
    prefix_set = {p.get("prefix", "").lower() for p in confirmed_prefixes}
    tables_with_data = {
        t for t, c in row_counts.items()
        if c and c != 0 and c != "error" and c != "100000+"
    } | {
        t for t, c in row_counts.items()
        if c == "100000+"
    }

    alignments = []

    for proc in processes:
        proc_name = proc if isinstance(proc, str) else proc.get("name", str(proc))
        proc_lower = proc_name.lower().replace(" ", "_").replace("-", "_")

        keywords = [kw for kw in proc_lower.split("_") if len(kw) > 3]
        matching_tables = [
            t for t in tables_lower
            if any(kw in t for kw in keywords)
        ]
        matching_prefixes = [
            p for p in prefix_set
            if any(kw in p for kw in keywords)
        ]

        if matching_tables:
            has_data = any(t in tables_with_data for t in matching_tables)
            status = "exists_with_data" if has_data else "exists_empty"
        elif matching_prefixes:
            status = "exists_in_storage"
        else:
            status = "no_backing_data"

        alignments.append({
            "process": proc_name,
            "matched_tables": matching_tables,
            "matched_prefixes": matching_prefixes,
            "status": status,
        })

    return alignments


# ==============================================================================
# Schema snapshot output
# ==============================================================================

def write_schema_snapshot(
    project_path: Path,
    database_results: list[dict],
    storage_results: list[dict],
    schema_authority: str,
    api_results: list[dict] | None = None,
) -> Path:
    """
    Write confirmed schema snapshot to .aah/architecture/data-schema-snapshot.yaml.
    This becomes the schema contract for downstream phases.

    api_results: optional list of {service_type, display_name, datasets, projects}
    entries for SaaS APIs (e.g. LangSmith) that data-readiness validated.
    Datasets/projects items carry {id, name, example_count?}. Plan phase
    reads the resulting 'apis' section to wire features against the exact
    validated resource references.
    """
    aah_path = project_path / ".aah"
    arch_path = aah_path / "architecture"
    arch_path.mkdir(parents=True, exist_ok=True)
    output_path = arch_path / "data-schema-snapshot.yaml"

    databases = []
    for db_result in database_results:
        tables = []
        actual_columns = db_result.get("result", {}).get("actual_columns", {})
        row_counts_data = db_result.get("result", {}).get("row_counts", {})

        for table_name, columns in actual_columns.items():
            tables.append({
                "name": table_name,
                "columns": [{"name": c["name"], "type": c["type"]} for c in columns],
                "row_count": row_counts_data.get(table_name, 0),
            })

        databases.append({
            "service_type": db_result.get("service_type", ""),
            "display_name": db_result.get("display_name", ""),
            "schema": db_result.get("config", {}).get("schema", "public"),
            "tables": tables,
        })

    storage = []
    for st_result in storage_results:
        prefix_checks = st_result.get("result", {}).get("prefix_checks", [])
        storage.append({
            "service_type": st_result.get("service_type", ""),
            "display_name": st_result.get("display_name", ""),
            "bucket_name": st_result.get("config", {}).get("bucket_name", ""),
            "prefixes": [
                {
                    "prefix": pc["prefix"],
                    "file_count": pc["actual_count"],
                    "file_types": pc["file_types_filter"],
                    "sample_keys": pc.get("sample_keys", []),
                }
                for pc in prefix_checks
            ],
        })

    apis = []
    for api_result in (api_results or []):
        api_entry = {
            "service_type": api_result.get("service_type", ""),
            "display_name": api_result.get("display_name", ""),
        }
        # Dedupe by id (preserve order)
        seen_ds = set()
        ds_list = []
        for ds in api_result.get("datasets", []):
            if not isinstance(ds, dict):
                continue
            key = ds.get("id") or ds.get("name")
            if not key or key in seen_ds:
                continue
            seen_ds.add(key)
            ds_entry = {"id": ds.get("id"), "name": ds.get("name")}
            if "example_count" in ds:
                ds_entry["example_count"] = ds["example_count"]
            ds_list.append(ds_entry)
        if ds_list:
            api_entry["datasets"] = ds_list

        seen_pj = set()
        pj_list = []
        for pj in api_result.get("projects", []):
            if not isinstance(pj, dict):
                continue
            key = pj.get("id") or pj.get("name")
            if not key or key in seen_pj:
                continue
            seen_pj.add(key)
            pj_list.append({"id": pj.get("id"), "name": pj.get("name")})
        if pj_list:
            api_entry["projects"] = pj_list

        apis.append(api_entry)

    snapshot = {
        "schema_version": "1.0",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "schema_authority": schema_authority,
        "databases": databases,
        "storage": storage,
        "apis": apis,
    }

    write_yaml(snapshot, output_path)
    return output_path


def _refresh_schema_snapshot_with_check(
    project_path: Path,
    service_type: str,
    check_record: dict,
) -> Path | None:
    """
    Merge a single passed check into .aah/architecture/data-schema-snapshot.yaml
    without losing other previously-captured entries. Called automatically after a
    successful --validate run.

    For SQL/storage services we already capture via the explicit
    --write-snapshot flow; this helper is mainly here for new categories
    (LangSmith and similar APIs) where the snapshot is the only place plan
    phase will look.
    """
    aah_path = project_path / ".aah"
    snapshot_path = aah_path / "architecture" / "data-schema-snapshot.yaml"

    existing = read_yaml(snapshot_path) if snapshot_path.exists() else {}
    if not isinstance(existing, dict):
        existing = {}

    databases = list(existing.get("databases") or [])
    storage = list(existing.get("storage") or [])
    apis = list(existing.get("apis") or [])

    category = check_record.get("category")
    result = check_record.get("result", {}) or {}

    if category == "api" and service_type == "langsmith":
        datasets = []
        for d in result.get("datasets_found", []):
            if not isinstance(d, dict):
                continue
            entry = {"id": d.get("id"), "name": d.get("name")}
            row_count = (result.get("row_counts") or {}).get(str(d.get("id")))
            if row_count is not None:
                entry["example_count"] = row_count
            datasets.append(entry)
        projects = [
            {"id": p.get("id"), "name": p.get("name")}
            for p in result.get("projects_found", [])
            if isinstance(p, dict)
        ]
        # Replace any prior langsmith entry rather than duplicate
        apis = [a for a in apis if a.get("service_type") != "langsmith"]
        apis.append({
            "service_type": "langsmith",
            "display_name": check_record.get("display_name", "LangSmith"),
            "datasets": datasets,
            "projects": projects,
        })
    elif category == "database":
        # Reuse existing write_schema_snapshot logic by feeding through it
        # — but keep storage/apis intact.
        actual_columns = result.get("actual_columns", {}) or {}
        row_counts = result.get("row_counts", {}) or {}
        tables = []
        for table_name, columns in actual_columns.items():
            tables.append({
                "name": table_name,
                "columns": [{"name": c["name"], "type": c["type"]} for c in columns],
                "row_count": row_counts.get(table_name, 0),
            })
        cfg = check_record.get("config") or {}
        db_entry = {
            "service_type": service_type,
            "display_name": check_record.get("display_name", ""),
            "schema": cfg.get("schema", "public"),
            "tables": tables,
        }
        databases = [d for d in databases if d.get("service_type") != service_type]
        databases.append(db_entry)
    elif category == "storage":
        prefix_checks = result.get("prefix_checks", []) or []
        cfg = check_record.get("config") or {}
        st_entry = {
            "service_type": service_type,
            "display_name": check_record.get("display_name", ""),
            "bucket_name": cfg.get("bucket_name", ""),
            "prefixes": [
                {
                    "prefix": pc["prefix"],
                    "file_count": pc["actual_count"],
                    "file_types": pc["file_types_filter"],
                    "sample_keys": pc.get("sample_keys", []),
                }
                for pc in prefix_checks
            ],
        }
        storage = [s for s in storage if s.get("service_type") != service_type]
        storage.append(st_entry)
    else:
        return None

    snapshot = {
        "schema_version": existing.get("schema_version", "1.0"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "schema_authority": existing.get("schema_authority", "live-validation"),
        "databases": databases,
        "storage": storage,
        "apis": apis,
    }
    aah_path.mkdir(parents=True, exist_ok=True)
    write_yaml(snapshot, snapshot_path)
    return snapshot_path


# ==============================================================================
# Dispatch
# ==============================================================================

# ==============================================================================
# Single source of truth for data-bearing services.
# Adding a new service: append one row here. No other edits needed.
#
# Schema:
#   service_type -> (kind: "database"|"storage", category, handler)
#   - kind drives _is_database / _is_storage classification
#   - category is the dispatch key inside run_check (selects expectation shape)
#   - handler is the function called for this service_type
# ==============================================================================

_DATA_SERVICE_REGISTRY: dict[str, tuple[str, str, callable]] = {
    # Keys match the `handler_id` values in
    # aah/_resources/access/cloud-service-catalog.yaml. Seeded services
    # carry service_type = handler_id, so the join is one-to-one.

    # Postgres family
    "aws-rds-postgres":       ("database", "postgres",   check_postgres_tables),
    "azure-postgres":         ("database", "postgres",   check_postgres_tables),
    "gcp-cloud-sql-postgres": ("database", "postgres",   check_postgres_tables),
    "postgres-generic":       ("database", "postgres",   check_postgres_tables),

    # Document / NoSQL
    "aws-dynamodb":           ("database", "dynamodb",   check_dynamodb_tables),
    "mongodb-generic":        ("database", "mongodb",    None),   # classification only
    "neo4j":                  ("database", "neo4j",      None),   # classification only
    "snowflake":              ("database", "snowflake",  None),   # classification only

    # Object storage
    "aws-s3":                 ("storage",  "s3",         check_s3_files),
    "gcp-gcs":                ("storage",  "gcs",        check_gcs_files),
    "azure-blob":             ("storage",  "azure-blob", check_azure_blob_files),

    # SaaS APIs
    "langsmith":              ("api",      "langsmith",  check_langsmith_datasets),
}

DATABASE_SERVICE_TYPES = frozenset(
    st for st, (kind, _, _) in _DATA_SERVICE_REGISTRY.items() if kind == "database"
)
STORAGE_SERVICE_TYPES = frozenset(
    st for st, (kind, _, _) in _DATA_SERVICE_REGISTRY.items() if kind == "storage"
)
API_SERVICE_TYPES = frozenset(
    st for st, (kind, _, _) in _DATA_SERVICE_REGISTRY.items() if kind == "api"
)

CHECK_DISPATCH = {
    st: (cat, handler)
    for st, (_, cat, handler) in _DATA_SERVICE_REGISTRY.items()
    if handler is not None
}


# ==============================================================================
# State management
# ==============================================================================

def load_data_readiness(project_path: Path) -> dict | None:
    path = project_path / ".aah" / "architecture" / "data-readiness.yaml"
    if path.exists():
        return read_yaml(path)
    return None


def save_data_readiness(project_path: Path, state: dict) -> Path:
    aah_path = project_path / ".aah"
    arch_path = aah_path / "architecture"
    arch_path.mkdir(parents=True, exist_ok=True)
    output_path = arch_path / "data-readiness.yaml"
    state["validated_at"] = datetime.now(timezone.utc).isoformat()
    write_yaml(state, output_path)
    return output_path


def extract_data_services(project_path: Path) -> list[dict]:
    """
    Extract database and storage services from cloud-readiness.yaml
    that passed connectivity (status=pass or override).
    """
    cloud_readiness_path = project_path / ".aah" / "architecture" / "cloud-readiness.yaml"
    if not cloud_readiness_path.exists():
        return []

    state = read_yaml(cloud_readiness_path)
    services = state.get("services", [])

    data_services = []
    for svc in services:
        svc_type = svc.get("type") or svc.get("service_type", "")
        status = svc.get("status", "")

        if status not in ("pass", "override"):
            continue

        if _is_database(svc_type) or _is_storage(svc_type) or _is_api(svc_type):
            if _is_database(svc_type):
                category = "database"
            elif _is_storage(svc_type):
                category = "storage"
            else:
                category = "api"
            data_services.append({
                "service_type": svc_type,
                "display_name": svc.get("display_name") or svc.get("name", svc_type),
                "category": category,
                "config": {
                    k: v for k, v in svc.items()
                    if k not in ("id", "name", "display_name", "type", "service_type",
                                 "criticality", "source_ddr", "status", "latency_ms",
                                 "validated_at", "error", "note")
                },
                "source_ddr": svc.get("source_ddr", ""),
                "criticality": svc.get("criticality", "advisory"),
            })

    return data_services


def run_check(
    service_type: str,
    config: dict,
    expectations: dict,
    project_path: Path | None = None,
) -> dict:
    """
    Run a single data readiness check for a service.

    Modes (Postgres / DynamoDB):
      1. expectations.expected_tables provided -> validate exactly those
      2. expectations.use_case_tables provided -> validate those AND mark
         them as "use-case-scoped" in the result
      3. neither -> skip with note (caller should run --suggest-tables first)

    Modes (LangSmith):
      1. expectations.dataset_ids / dataset_names / project_ids / project_names
         provided inline -> validate exactly those (highest priority)
      2. knowledge/langsmith-datasets.{yaml,yml,json} or knowledge/langsmith.*
         present -> load and validate listed datasets/projects
      3. neither -> skip with note (caller should run --suggest-langsmith-datasets)

    project_path is required for the knowledge-file fallback to work for
    LangSmith. If omitted, the inline-only path still works.
    """
    if service_type not in CHECK_DISPATCH:
        return {"passed": True, "latency_ms": 0, "note": "No data check handler for this type"}

    category, handler_fn = CHECK_DISPATCH[service_type]

    if category == "postgres":
        tables = expectations.get("expected_tables") or expectations.get("use_case_tables") or []
        if not tables:
            return {
                "passed": True, "latency_ms": 0,
                "note": (
                    "No tables specified. Either: (a) place a schema file in "
                    "knowledge/, or (b) re-run with --suggest-tables to pick "
                    "use-case tables from the live database."
                ),
            }
        scope = "schema-file" if expectations.get("expected_tables") else "use-case-only"
        expected_columns = expectations.get("expected_columns", {})
        min_row_counts = expectations.get("min_row_counts", {})
        result = handler_fn(config, tables, expected_columns, min_row_counts)
        result["validation_scope"] = scope
        result["validated_tables"] = list(tables)
        return result
    elif category == "dynamodb":
        tables = expectations.get("expected_tables") or expectations.get("use_case_tables") or []
        if not tables:
            return {
                "passed": True, "latency_ms": 0,
                "note": "No tables specified. Re-run with --suggest-tables to pick use-case tables.",
            }
        scope = "schema-file" if expectations.get("expected_tables") else "use-case-only"
        # Optional: pull per-table shape from knowledge/dynamodb-schema.yaml.
        # Only tables that appear in `tables` are used; extras in the yaml are ignored.
        expected_schemas = expectations.get("expected_schemas") or {}
        if not expected_schemas and project_path is not None:
            all_schemas = _extract_dynamodb_schemas(project_path / "knowledge")
            expected_schemas = {t: all_schemas[t] for t in tables if t in all_schemas}
        result = handler_fn(config, tables, expected_schemas or None)
        result["validation_scope"] = scope
        result["validated_tables"] = list(tables)
        return result
    elif category in ("s3", "gcs", "azure-blob"):
        prefixes = expectations.get("expected_prefixes", [])
        if not prefixes:
            return {"passed": True, "latency_ms": 0, "note": "No file expectations specified  -- skipping"}
        return handler_fn(config, prefixes)
    elif category == "langsmith":
        dataset_ids = list(expectations.get("dataset_ids") or [])
        dataset_names = list(expectations.get("dataset_names") or [])
        project_ids = list(expectations.get("project_ids") or [])
        project_names = list(expectations.get("project_names") or [])
        min_example_counts = dict(expectations.get("min_example_counts") or {})
        default_min = int(expectations.get("default_min_examples", 1))

        scope = "inline"
        knowledge_meta: dict | None = None

        # Fallback: load knowledge file if no inline expectations were given.
        inline_provided = bool(
            dataset_ids or dataset_names or project_ids or project_names
        )
        if not inline_provided and project_path is not None:
            knowledge_meta = _load_langsmith_knowledge_file(project_path)
            if knowledge_meta:
                scope = "knowledge-file"
                # Pull file-level default if caller didn't specify
                if "default_min_examples" not in expectations:
                    default_min = int(knowledge_meta.get("default_min_examples") or default_min)

                for ds in knowledge_meta.get("datasets", []):
                    if ds.get("id"):
                        dataset_ids.append(ds["id"])
                    if ds.get("name"):
                        dataset_names.append(ds["name"])
                    # Per-dataset min_examples — keyed by id if present, else name
                    if ds.get("min_examples") is not None:
                        key = ds.get("id") or ds.get("name")
                        if key:
                            min_example_counts.setdefault(key, int(ds["min_examples"]))

                for pj in knowledge_meta.get("projects", []):
                    if pj.get("id"):
                        project_ids.append(pj["id"])
                    if pj.get("name"):
                        project_names.append(pj["name"])

        if not (dataset_ids or dataset_names or project_ids or project_names):
            return {
                "passed": True, "latency_ms": 0,
                "validation_scope": "use-case-only-not-yet-picked",
                "note": (
                    "No LangSmith expectations supplied. Either: "
                    "(a) place knowledge/langsmith-datasets.{yaml,yml,json}, "
                    "(b) pass dataset_ids/names/project_ids/names inline via --validate, "
                    "(c) run --suggest-langsmith-datasets to list everything in your workspace."
                ),
            }

        result = handler_fn(
            config,
            expected_ids=dataset_ids,
            expected_names=dataset_names,
            min_example_counts=min_example_counts,
            default_min=default_min,
            expected_project_ids=project_ids,
            expected_project_names=project_names,
        )
        result["validation_scope"] = scope
        if knowledge_meta:
            result["knowledge_source_path"] = knowledge_meta.get("source_path")
        return result

    return {"passed": True, "latency_ms": 0, "note": "Unhandled category"}


# ==============================================================================
# CLI
# ==============================================================================

def main() -> None:
    from aah.core.gates.readiness_banner import print_banner
    print_banner("data")

    from aah.core.common.runguard import require_rapids_run
    require_rapids_run("validate_data_readiness")

    parser = argparse.ArgumentParser(description="Data Readiness Validation Gate")
    parser.add_argument("--project-path", type=Path, required=True)
    parser.add_argument(
        "--check-state", action="store_true",
        help="Check existing data-readiness.yaml state"
    )
    parser.add_argument(
        "--discover-sources", action="store_true",
        help="Discover schema/data sources from knowledge/ and codebase-intel/"
    )
    parser.add_argument(
        "--suggest-tables", type=str, default=None,
        help=(
            "Live-discovery mode (no schema file required). JSON: "
            '{"service_type":"...", "config":{...}}. Lists every table in '
            "the live DB, scored by overlap with project keywords from "
            "knowledge/ and the decision registry, so the user can pick "
            "use-case tables to validate."
        ),
    )
    parser.add_argument(
        "--suggest-langsmith-datasets", type=str, default=None,
        help=(
            "Live-discovery mode for LangSmith. JSON: "
            '{"config":{"api_url":"...","auth_method":"secret-manager",'
            '"provider":"gcp","secret_path":"..."}}. Lists every dataset and '
            "project visible to the API key with example counts, so the user "
            "can pick names/IDs to validate. Reports whether a "
            "knowledge/langsmith-datasets.* file is already present."
        ),
    )
    parser.add_argument(
        "--extract-services", action="store_true",
        help="Extract data-bearing services from cloud-readiness.yaml"
    )
    parser.add_argument(
        "--validate", type=str, default=None,
        help="JSON config for a single service check: {service_type, config, expectations}"
    )
    parser.add_argument(
        "--write-snapshot", type=str, default=None,
        help="JSON with {database_results, storage_results, schema_authority} to write snapshot"
    )
    parser.add_argument(
        "--check-alignment", action="store_true",
        help="Run use-case alignment check against registry domain_scope"
    )
    parser.add_argument(
        "--skip", action="store_true",
        help="Skip data readiness gate with a reason"
    )
    parser.add_argument("--skip-reason", type=str, default="User deferred data readiness validation")
    parser.add_argument(
        "--override-service", type=str, default=None,
        help="Override a specific service (mark as accepted despite failures)"
    )
    parser.add_argument("--override-reason", type=str, default=None)
    parser.add_argument("--profile", type=str, default=None, help="AWS CLI profile")
    parser.add_argument(
        "--show-identities", action="store_true",
        help="Print the principal RAPIDS authenticated as on every cloud, then exit"
    )
    parser.add_argument(
        "--require-cloud-gate", action="store_true",
        help=(
            "Require /aah-access to have emitted .aah/architecture/cloud-readiness.yaml "
            "with gate_status=passed (or skipped) before running data-readiness checks. "
            "Set by /aah-data skill invocations. Halts on missing/stale/failed."
        )
    )

    args = parser.parse_args()
    project_path = resolve_project_path(args.project_path)
    if project_path is None:
        print(json.dumps({"error": "Could not resolve project path"}))
        sys.exit(1)

    if args.show_identities:
        from aah.core.cloud.identity import all_identities
        identities = all_identities(aws_profile=args.profile)
        for ident in identities:
            print_identity_banner(ident, stream=sys.stdout)
        print(json.dumps(identities, indent=2, default=str))
        sys.exit(0)

    if args.check_state:
        state = load_data_readiness(project_path)
        if state:
            print(json.dumps(state, default=str))
        else:
            print(json.dumps({"gate_status": "not-started", "checks": []}))
        sys.exit(0)

    if args.discover_sources:
        sources = discover_schema_sources(project_path)
        print(json.dumps({
            "sources_found": len(sources),
            "sources": sources,
        }, default=str))
        sys.exit(0)

    if args.suggest_tables:
        try:
            cfg = json.loads(args.suggest_tables)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --suggest-tables JSON: {e}"}))
            sys.exit(1)
        service_type = cfg.get("service_type", "")
        service_config = cfg.get("config", {})
        if args.profile:
            service_config["aws_profile"] = args.profile

        keywords = discover_project_keywords(project_path)
        sources = discover_schema_sources(project_path)
        schema_present = bool(sources)

        if service_type in {"rds-postgres", "azure-postgres", "cloud-sql-postgres",
                            "postgres-generic"}:
            live_tables = list_live_tables_postgres(service_config)
        else:
            print(json.dumps({
                "error": (
                    f"--suggest-tables not yet implemented for service_type="
                    f"{service_type}. Currently supports postgres-family only."
                )
            }))
            sys.exit(1)

        suggestions = suggest_use_case_tables(live_tables, keywords)
        scored = [s for s in suggestions if s["score"] > 0]
        unscored = [s for s in suggestions if s["score"] == 0]

        print(json.dumps({
            "service_type": service_type,
            "schema_file_present": schema_present,
            "schema_files": [s["path"] for s in sources],
            "project_keywords_used": keywords[:30],
            "project_keywords_total": len(keywords),
            "live_tables_total": len(live_tables),
            "suggested_use_case_tables": scored,
            "other_tables": unscored,
            "next_step": (
                "Pass the chosen tables back via --validate with "
                'expectations.use_case_tables = ["table1", "table2", ...]'
            ),
        }, indent=2, default=str))
        sys.exit(0)

    if args.suggest_langsmith_datasets:
        # Live-discovery for LangSmith — list datasets + projects visible to
        # the API key, with example counts. Read-only.
        try:
            cfg = json.loads(args.suggest_langsmith_datasets)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --suggest-langsmith-datasets JSON: {e}"}))
            sys.exit(1)

        service_config = cfg.get("config", {})
        api_url = (service_config.get("api_url") or "").rstrip("/")
        if not api_url:
            print(json.dumps({"error": "config.api_url is required"}))
            sys.exit(1)
        api_key = _langsmith_resolve_api_key(service_config)
        if not api_key:
            print(json.dumps({
                "error": (
                    "No LangSmith API key resolvable. Pass auth_method=secret-manager "
                    "with provider=gcp + secret_path, or set LANGSMITH_API_KEY env var."
                )
            }))
            sys.exit(1)

        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
        headers = {
            "x-api-key": api_key,
            "User-Agent": "aah-data-readiness/1.0",
        }

        def _suggest_get(path: str):
            url = f"{api_url}{path}"
            try:
                resp = urlopen(Request(url, headers=headers, method="GET"), timeout=15)
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.getcode(), json.loads(body), None
                except ValueError:
                    return resp.getcode(), None, "non-JSON response"
            except HTTPError as e:
                return e.code, None, f"HTTP {e.code}: {e.reason}"
            except URLError as e:
                return 0, None, f"Network error: {e.reason}"
            except Exception as e:
                return 0, None, str(e)

        def _items(body):
            if isinstance(body, list):
                return body
            if isinstance(body, dict):
                return body.get("items", []) or []
            return []

        # Datasets — list and probe each for example count (capped at 50)
        ds_status, ds_body, ds_err = _suggest_get("/api/v1/datasets?limit=100")
        datasets = []
        if ds_err:
            datasets_error = ds_err
        else:
            datasets_error = None
            for ds in _items(ds_body):
                if not isinstance(ds, dict):
                    continue
                did = ds.get("id")
                if not did:
                    continue
                cnt_status, cnt_body, cnt_err = _suggest_get(f"/api/v1/examples?dataset={did}&limit=51")
                if cnt_err:
                    example_count = "error"
                else:
                    items = _items(cnt_body)
                    n = len(items)
                    example_count = n if n < 51 else ">=51"
                datasets.append({
                    "id": did,
                    "name": ds.get("name"),
                    "description": ds.get("description") or "",
                    "example_count": example_count,
                })

        # Projects (sessions) — list only, no count probe (sessions are open-ended)
        pj_status, pj_body, pj_err = _suggest_get("/api/v1/sessions?limit=100")
        projects = []
        if pj_err:
            projects_error = pj_err
        else:
            projects_error = None
            for pj in _items(pj_body):
                if not isinstance(pj, dict):
                    continue
                projects.append({
                    "id": pj.get("id"),
                    "name": pj.get("name"),
                })

        knowledge_meta = _load_langsmith_knowledge_file(project_path)

        # Drop the local key reference once probes are done.
        api_key = None

        print(json.dumps({
            "service_type": "langsmith",
            "api_url": api_url,
            "knowledge_file_present": knowledge_meta is not None,
            "knowledge_file_path": (knowledge_meta or {}).get("source_path"),
            "datasets_total": len(datasets),
            "datasets": datasets,
            "datasets_error": datasets_error,
            "projects_total": len(projects),
            "projects": projects,
            "projects_error": projects_error,
            "next_step": (
                "Either: (a) write the chosen names/ids into "
                "knowledge/langsmith-datasets.yaml (datasets:/projects: blocks), "
                "or (b) pass them inline to --validate via "
                'expectations.dataset_ids/dataset_names/project_ids/project_names.'
            ),
        }, indent=2, default=str))
        sys.exit(0)

    if args.extract_services:
        services = extract_data_services(project_path)
        print(json.dumps({
            "data_services": len(services),
            "services": services,
        }, default=str))
        sys.exit(0)

    if args.write_snapshot:
        try:
            snapshot_config = json.loads(args.write_snapshot)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --write-snapshot JSON: {e}"}))
            sys.exit(1)
        path = write_schema_snapshot(
            project_path,
            snapshot_config.get("database_results", []),
            snapshot_config.get("storage_results", []),
            snapshot_config.get("schema_authority", "live-database"),
            api_results=snapshot_config.get("api_results", []),
        )
        print(json.dumps({"outcome": "snapshot-written", "path": str(path)}))
        sys.exit(0)

    if args.check_alignment:
        state = load_data_readiness(project_path)
        if not state:
            print(json.dumps({"error": "No data-readiness.yaml  -- run validation first"}))
            sys.exit(1)
        confirmed_tables = []
        confirmed_prefixes = []
        row_counts = {}
        for check in state.get("checks", []):
            result = check.get("result", {})
            if check.get("category") == "database":
                confirmed_tables.extend(result.get("tables_found", []))
                row_counts.update(result.get("row_counts", {}))
            elif check.get("category") == "storage":
                for pc in result.get("prefix_checks", []):
                    confirmed_prefixes.append(pc)
        alignments = check_use_case_alignment(
            project_path, confirmed_tables, confirmed_prefixes, row_counts
        )
        print(json.dumps({"alignments": alignments}, default=str))
        sys.exit(0)

    # Handshake gate — only fires when invoked via /aah-data.
    # Existing callers (Analyze-phase orchestrator, rapids-analyze) omit the flag
    # and pass through unaffected. See _check_cloud_gate_handshake for exit semantics.
    if args.require_cloud_gate:
        _check_cloud_gate_handshake(project_path)

    if args.skip:
        state = {
            "schema_version": "1.0",
            "gate_status": "skipped",
            "skip_reason": args.skip_reason,
            "checks": [],
        }
        save_data_readiness(project_path, state)
        print(json.dumps({"outcome": "skipped", "reason": args.skip_reason}))
        sys.exit(0)

    if args.override_service:
        if not args.override_reason:
            print(json.dumps({"error": "--override-reason required"}), file=sys.stderr)
            sys.exit(1)
        state = load_data_readiness(project_path) or {
            "schema_version": "1.0", "checks": [], "overrides": [],
        }
        overrides = state.get("overrides", [])
        overrides.append({
            "service_type": args.override_service,
            "reason": args.override_reason,
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        })
        state["overrides"] = overrides
        for check in state.get("checks", []):
            if check.get("service_type") == args.override_service:
                check["status"] = "override"
        statuses = [c.get("status") for c in state.get("checks", [])]
        if all(s in ("pass", "override", "skipped") for s in statuses):
            state["gate_status"] = "passed"
        save_data_readiness(project_path, state)
        print(json.dumps({"outcome": "override-recorded", "service_type": args.override_service}))
        sys.exit(0)

    if args.validate:
        try:
            check_config = json.loads(args.validate)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --validate JSON: {e}"}))
            sys.exit(1)

        service_type = check_config.get("service_type", "")
        config = check_config.get("config", {})
        expectations = check_config.get("expectations", {})

        if args.profile:
            config["aws_profile"] = args.profile

        result = run_check(service_type, config, expectations, project_path=project_path)

        state = load_data_readiness(project_path) or {
            "schema_version": "1.0",
            "gate_status": "pending",
            "checks": [],
            "overrides": [],
        }
        checks = state.get("checks", [])
        existing_idx = next(
            (i for i, c in enumerate(checks) if c.get("service_type") == service_type),
            None,
        )
        check_record = {
            "service_type": service_type,
            "display_name": check_config.get("display_name", service_type),
            "category": check_config.get("category", "unknown"),
            "status": "pass" if result.get("passed") else "fail",
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        if existing_idx is not None:
            checks[existing_idx] = check_record
        else:
            checks.append(check_record)
        state["checks"] = checks

        statuses = [c.get("status") for c in checks]
        overridden_types = {o.get("service_type") for o in state.get("overrides", [])}
        effective = [
            s for s, c in zip(statuses, checks)
            if c.get("service_type") not in overridden_types
        ]
        if not effective or all(s in ("pass", "skipped") for s in effective):
            state["gate_status"] = "passed"
        else:
            state["gate_status"] = "pending"

        save_data_readiness(project_path, state)

        # Auto-update data-schema-snapshot.yaml when this run succeeded.
        # Plan phase reads that file to wire features against canonical
        # validated resources. We append the just-validated service to the
        # existing snapshot rather than overwriting other entries.
        if result.get("passed"):
            try:
                _refresh_schema_snapshot_with_check(
                    project_path, service_type, check_record
                )
            except Exception as exc:
                # Snapshot is best-effort; never block gate success on it.
                result["snapshot_warning"] = (
                    f"data-schema-snapshot.yaml refresh failed: {exc}"
                )

        result["service_type"] = service_type
        print(json.dumps(result, default=str))

        if not result.get("passed"):
            sys.exit(2)
        sys.exit(0)

    print(json.dumps({
        "error": "Specify --check-state, --discover-sources, --extract-services, "
                 "--validate, --write-snapshot, --check-alignment, --skip, or --override-service"
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
