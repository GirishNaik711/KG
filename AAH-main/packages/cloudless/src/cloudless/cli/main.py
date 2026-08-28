"""cloudless CLI dispatcher (Q30 command catalog).

M1 ships: `init`, `version`, `--help`.
Later milestones add: dev, deploy, rollback, logs, cost, eval, etc.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from cloudless._version import __version__
from cloudless.cli import init as init_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudless",
        description='cloudless — "Write your agent once. Ship it to any cloud."',
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"cloudless {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # dev
    p_dev = sub.add_parser(
        "dev",
        help="Run an agent locally (Q13) with real LLM + in-memory ctx.",
    )
    p_dev.add_argument(
        "agent_name", nargs="?", default=None,
        help="Agent name as declared in cloudless.yaml (omit with --all).",
    )
    p_dev.add_argument(
        "--all", dest="all_agents", action="store_true",
        help="Spawn every declared agent on consecutive ports + local manifest.",
    )
    p_dev.add_argument(
        "--host", default="127.0.0.1",
        help="Bind host (default: 127.0.0.1).",
    )
    p_dev.add_argument(
        "--port", type=int, default=8080,
        help="Bind port (default: 8080; matches AgentCore HTTP contract).",
    )
    p_dev.add_argument(
        "--reload", action="store_true",
        help="Watch src/agents for changes and respawn the dev server.",
    )
    p_dev_mode = p_dev.add_mutually_exclusive_group()
    p_dev_mode.add_argument(
        "--record", metavar="CASSETTE",
        help="Path to a cassette JSONL: real LLM calls are made AND recorded.",
    )
    p_dev_mode.add_argument(
        "--replay", metavar="CASSETTE",
        help="Path to a cassette JSONL: LLM calls served from cassette (no real cloud).",
    )

    # deploy
    p_deploy = sub.add_parser(
        "deploy",
        help="Deploy an agent to AgentCore (Q4 + Q6).",
    )
    p_deploy.add_argument("agent_name", help="Agent name as declared in cloudless.yaml.")
    p_deploy.add_argument(
        "--region", default="us-east-1",
        help="AWS region for the deploy (default: us-east-1).",
    )
    p_deploy.add_argument(
        "--build-dir", default=None,
        help="Where to materialize the artifact (default: .cloudless/build/<name>).",
    )

    # eval
    p_eval = sub.add_parser("eval", help="Run an eval dataset against an LLM (Q8).")
    p_eval_sub = p_eval.add_subparsers(dest="eval_command", required=True)
    p_eval_run = p_eval_sub.add_parser("run", help="Run an eval dataset.")
    p_eval_run.add_argument("dataset_path", help="Path to JSONL dataset.")
    p_eval_run.add_argument("--output", "-o", default=None,
                             help="Where to write results JSONL.")
    p_eval_run.add_argument("--model", default="nova-micro",
                             help="LLM alias for the target (default: nova-micro).")
    p_eval_run.add_argument("--region", default="us-east-1")

    # logs
    p_logs = sub.add_parser("logs", help="Stream a deployed agent's CloudWatch logs.")
    p_logs.add_argument("agent_name")
    p_logs.add_argument("--region", default="us-east-1")
    p_logs.add_argument("--since", default="10m", help="e.g. 10m, 1h, 24h, 7d")
    p_logs.add_argument("--follow", "-f", action="store_true")
    p_logs.add_argument("--endpoint", default="DEFAULT")
    p_logs.add_argument("--trace-id", dest="trace_id", default=None,
                         help="Only show lines whose structlog trace.id matches.")
    p_logs.add_argument("--session-id", dest="session_id", default=None,
                         help="Only show lines whose structlog session.id matches.")
    p_logs.add_argument("--level", default=None,
                         help="Minimum log level (DEBUG/INFO/WARNING/ERROR).")
    p_logs.add_argument("--json", dest="output_json", action="store_true",
                         help="Emit one JSON object per line instead of text.")

    # versions
    p_ver = sub.add_parser("versions", help="List versions + endpoint aliases for an agent.")
    p_ver.add_argument("agent_name")
    p_ver.add_argument("--region", default="us-east-1")

    # rollback
    p_rb = sub.add_parser("rollback", help="Roll an endpoint alias back to a prior version.")
    p_rb.add_argument("agent_name")
    p_rb.add_argument("--to", dest="to_version", default=None,
                      help="Target version (default: 2nd-most-recent).")
    p_rb.add_argument("--endpoint", default="DEFAULT")
    p_rb.add_argument("--region", default="us-east-1")

    # cost
    p_cost = sub.add_parser(
        "cost",
        help="Roll up LLM cost from event JSONL (stdin) or cassette files.",
    )
    p_cost.add_argument(
        "--by", choices=["model", "team"], default="model",
        help="Group rollup by (default: model).",
    )
    p_cost.add_argument(
        "--cassette", action="append", default=None,
        help="Glob for cassette JSONL files (repeatable).",
    )
    p_cost.add_argument(
        "--format", choices=["table", "json"], default="table",
        dest="output_format",
        help="Output format (default: table).",
    )

    # cleanup
    p_cl = sub.add_parser(
        "cleanup",
        help="Namespace-scoped teardown of cloudless cloud resources (DANGER).",
    )
    p_cl.add_argument("--prefix", required=True,
                       help="Resource name prefix to match (min 8 chars).")
    p_cl.add_argument("--aws", action="store_true", default=False,
                       help="Include AWS resources.")
    p_cl.add_argument("--gcp", action="store_true", default=False,
                       help="Include GCP resources (requires --gcp-project).")
    p_cl.add_argument("--aws-region", default="us-east-1")
    p_cl.add_argument("--gcp-project", default=None)
    p_cl.add_argument("--yes", action="store_true",
                       help="Actually delete (default is dry-run).")

    # security
    p_sec = sub.add_parser(
        "security",
        help="SBOM + dependency audit (Q33 pre-1.0 prep).",
    )
    p_sec_sub = p_sec.add_subparsers(dest="security_command", required=True)
    p_sec_sbom = p_sec_sub.add_parser("sbom", help="Generate CycloneDX 1.4 JSON SBOM.")
    p_sec_sbom.add_argument("--output", "-o", default=None,
                             help="Write SBOM to file (default: stdout).")
    p_sec_audit = p_sec_sub.add_parser(
        "audit", help="Run pip-audit against the installed deps.",
    )
    p_sec_audit.add_argument("--json", action="store_true", dest="json_output",
                              help="Output JSON instead of human text.")

    # doctor
    p_doc = sub.add_parser(
        "doctor",
        help="Run preflight environment checks (Q30: F1/F5/F15/F16/F17 hazards).",
    )
    p_doc.add_argument("-v", "--verbose", action="store_true",
                        help="Show detailed check output.")

    # init
    p_init = sub.add_parser(
        "init",
        help="Scaffold a new cloudless project (Q24 project layout).",
    )
    p_init.add_argument("project_name", help="Project directory to create.")
    p_init.add_argument(
        "--framework",
        choices=["langgraph", "strands"],
        default="langgraph",
        help="Agent framework for the scaffolded example (default: langgraph).",
    )
    p_init.add_argument(
        "--cloud",
        choices=["aws", "gcp"],
        default="aws",
        help="Default deploy cloud (M1: aws only).",
    )
    p_init.add_argument(
        "--force", action="store_true",
        help="Overwrite project directory if it exists.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on failure."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return init_cmd.run(
            project_name=args.project_name,
            framework=args.framework,
            cloud=args.cloud,
            force=args.force,
        )

    if args.command == "dev":
        if args.all_agents:
            from cloudless.cli import dev_all
            return dev_all.run(base_port=args.port)
        if not args.agent_name:
            parser.error("dev: agent_name required unless --all is given")
        from cloudless.cli import dev as dev_cmd
        return dev_cmd.run(
            agent_name=args.agent_name,
            host=args.host,
            port=args.port,
            record_cassette=args.record,
            replay_cassette=args.replay,
            reload=args.reload,
        )

    if args.command == "eval":
        if args.eval_command == "run":
            from cloudless.cli import eval as eval_cmd
            return eval_cmd.run(
                dataset_path=args.dataset_path,
                output_path=args.output,
                model=args.model,
                region=args.region,
            )

    if args.command == "logs":
        from cloudless.cli import ops
        return ops.logs_command(
            agent_name=args.agent_name, region=args.region,
            since=args.since, follow=args.follow, endpoint=args.endpoint,
            trace_id=args.trace_id, session_id=args.session_id,
            level=args.level,
            output="json" if args.output_json else "text",
        )

    if args.command == "versions":
        from cloudless.cli import ops
        return ops.versions_command(agent_name=args.agent_name, region=args.region)

    if args.command == "rollback":
        from cloudless.cli import ops
        return ops.rollback_command(
            agent_name=args.agent_name, to_version=args.to_version,
            endpoint=args.endpoint, region=args.region,
        )

    if args.command == "cost":
        from cloudless.cli import cost as cost_cmd
        return cost_cmd.run(
            by=args.by,
            cassette_globs=args.cassette,
            output_format=args.output_format,
        )

    if args.command == "cleanup":
        from cloudless.cli import cleanup as cleanup_cmd
        # Default both clouds off → require explicit opt-in
        aws = args.aws or (not args.aws and not args.gcp)
        return cleanup_cmd.run(
            prefix=args.prefix,
            aws=aws,
            gcp=args.gcp,
            aws_region=args.aws_region,
            gcp_project=args.gcp_project,
            dry_run=not args.yes,
            yes=args.yes,
        )

    if args.command == "security":
        from cloudless.cli import security as security_cmd
        if args.security_command == "sbom":
            return security_cmd.sbom_command(output_path=args.output)
        if args.security_command == "audit":
            return security_cmd.audit_command(json_output=args.json_output)

    if args.command == "doctor":
        from cloudless.cli import doctor as doctor_cmd
        return doctor_cmd.run(verbose=args.verbose)

    if args.command == "deploy":
        from cloudless.cli import deploy as deploy_cmd
        return deploy_cmd.run(
            agent_name=args.agent_name,
            region=args.region,
            build_dir=Path(args.build_dir) if args.build_dir else None,
        )

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable; parser.error sys.exits


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
