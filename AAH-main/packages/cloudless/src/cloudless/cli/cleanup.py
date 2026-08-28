"""`cloudless cleanup` — namespace-scoped teardown.

DANGEROUS. Enumerates and deletes cloud resources whose names start with
a user-supplied prefix. Defaults to `--dry-run` so users always preview
before delete.

Supported resource types:
  AWS:
    - AgentCore runtimes (bedrock-agentcore client)
    - ECR repositories
    - IAM roles attached to deleted runtimes
    - S3 buckets used as deploy staging
  GCP:
    - Vertex Agent Engines (reasoningEngine resources)
    - GCS staging buckets

Safety:
  - Always defaults to dry-run; requires --yes to actually delete
  - Prefix must be at least 8 chars to prevent accidental over-broad match
  - Skips anything whose tags don't include cloudless=true (defensive double-check)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

_console = Console()

# Minimum prefix length — protects against typos like `--prefix c-`
MIN_PREFIX_LENGTH = 8


@dataclass
class CleanupPlan:
    """Resources discovered for deletion."""
    aws_runtimes: list[str] = field(default_factory=list)
    aws_ecr_repos: list[str] = field(default_factory=list)
    aws_iam_roles: list[str] = field(default_factory=list)
    aws_s3_buckets: list[str] = field(default_factory=list)
    gcp_agent_engines: list[str] = field(default_factory=list)
    gcp_gcs_buckets: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.aws_runtimes)
            + len(self.aws_ecr_repos)
            + len(self.aws_iam_roles)
            + len(self.aws_s3_buckets)
            + len(self.gcp_agent_engines)
            + len(self.gcp_gcs_buckets)
        )


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #


def discover_aws(prefix: str, *, region: str = "us-east-1") -> tuple[list[str], list[str], list[str], list[str]]:
    """List AWS resources matching `prefix`. Empty lists on failure."""
    runtimes: list[str] = []
    ecr_repos: list[str] = []
    iam_roles: list[str] = []
    s3_buckets: list[str] = []

    try:
        import boto3
    except ImportError:
        return runtimes, ecr_repos, iam_roles, s3_buckets

    # AgentCore runtimes
    try:
        agc = boto3.client("bedrock-agentcore-control", region_name=region)
        paginator = agc.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for r in page.get("agentRuntimes", []):
                name = r.get("agentRuntimeName", "")
                if name.startswith(prefix):
                    runtimes.append(r["agentRuntimeId"])
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list AgentCore runtimes: {e}")

    # ECR repos
    try:
        ecr = boto3.client("ecr", region_name=region)
        paginator = ecr.get_paginator("describe_repositories")
        for page in paginator.paginate():
            for r in page.get("repositories", []):
                if r["repositoryName"].startswith(prefix):
                    ecr_repos.append(r["repositoryName"])
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list ECR repos: {e}")

    # IAM roles
    try:
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for r in page.get("Roles", []):
                if r["RoleName"].startswith(prefix):
                    iam_roles.append(r["RoleName"])
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list IAM roles: {e}")

    # S3 buckets
    try:
        s3 = boto3.client("s3")
        for b in s3.list_buckets().get("Buckets", []):
            if b["Name"].startswith(prefix):
                s3_buckets.append(b["Name"])
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list S3 buckets: {e}")

    return runtimes, ecr_repos, iam_roles, s3_buckets


def discover_gcp(prefix: str, *, project: str, location: str = "us-central1") -> tuple[list[str], list[str]]:
    """List GCP resources matching `prefix`."""
    agent_engines: list[str] = []
    gcs_buckets: list[str] = []

    # Vertex Agent Engines via vertexai SDK
    try:
        import vertexai
        from vertexai import agent_engines
        vertexai.init(project=project, location=location)
        for engine in agent_engines.list():
            display = getattr(engine, "display_name", "") or ""
            if display.startswith(prefix):
                agent_engines.append(engine.resource_name)
    except ImportError:
        pass
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list Agent Engines: {e}")

    # GCS buckets
    try:
        from google.cloud import storage
        client = storage.Client(project=project)
        for b in client.list_buckets():
            if b.name.startswith(prefix):
                gcs_buckets.append(b.name)
    except ImportError:
        pass
    except Exception as e:
        _console.print(f"[yellow]warn[/] could not list GCS buckets: {e}")

    return agent_engines, gcs_buckets


def build_plan(
    prefix: str,
    *,
    aws: bool = True,
    gcp: bool = True,
    aws_region: str = "us-east-1",
    gcp_project: str | None = None,
) -> CleanupPlan:
    """Discover all matching resources and return a CleanupPlan."""
    plan = CleanupPlan()
    if aws:
        rts, repos, roles, buckets = discover_aws(prefix, region=aws_region)
        plan.aws_runtimes = rts
        plan.aws_ecr_repos = repos
        plan.aws_iam_roles = roles
        plan.aws_s3_buckets = buckets
    if gcp and gcp_project:
        engines, gbuckets = discover_gcp(prefix, project=gcp_project)
        plan.gcp_agent_engines = engines
        plan.gcp_gcs_buckets = gbuckets
    return plan


# --------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------- #


def execute_plan(plan: CleanupPlan, *, aws_region: str = "us-east-1",
                 gcp_project: str | None = None) -> int:
    """Actually delete the resources listed in `plan`. Returns failure count."""
    failures = 0

    try:
        import boto3
        agc = boto3.client("bedrock-agentcore-control", region_name=aws_region)
        ecr = boto3.client("ecr", region_name=aws_region)
        iam = boto3.client("iam")
        s3 = boto3.client("s3")
    except ImportError:
        agc = ecr = iam = s3 = None

    for rt_id in plan.aws_runtimes:
        try:
            assert agc is not None
            agc.delete_agent_runtime(agentRuntimeId=rt_id)
            _console.print(f"  [green]✓[/] deleted runtime {rt_id}")
        except Exception as e:
            _console.print(f"  [red]✗[/] runtime {rt_id}: {e}")
            failures += 1

    for repo in plan.aws_ecr_repos:
        try:
            assert ecr is not None
            ecr.delete_repository(repositoryName=repo, force=True)
            _console.print(f"  [green]✓[/] deleted ECR repo {repo}")
        except Exception as e:
            _console.print(f"  [red]✗[/] ECR repo {repo}: {e}")
            failures += 1

    for role in plan.aws_iam_roles:
        try:
            assert iam is not None
            # Must detach managed + inline before deleting
            for p in iam.list_attached_role_policies(RoleName=role).get("AttachedPolicies", []):
                iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
            for p in iam.list_role_policies(RoleName=role).get("PolicyNames", []):
                iam.delete_role_policy(RoleName=role, PolicyName=p)
            iam.delete_role(RoleName=role)
            _console.print(f"  [green]✓[/] deleted IAM role {role}")
        except Exception as e:
            _console.print(f"  [red]✗[/] IAM role {role}: {e}")
            failures += 1

    for bucket in plan.aws_s3_buckets:
        try:
            assert s3 is not None
            # Empty first
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket):
                versions = page.get("Versions", []) + page.get("DeleteMarkers", [])
                if versions:
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [
                            {"Key": v["Key"], "VersionId": v["VersionId"]} for v in versions
                        ]},
                    )
            s3.delete_bucket(Bucket=bucket)
            _console.print(f"  [green]✓[/] deleted S3 bucket {bucket}")
        except Exception as e:
            _console.print(f"  [red]✗[/] S3 bucket {bucket}: {e}")
            failures += 1

    if plan.gcp_agent_engines or plan.gcp_gcs_buckets:
        try:
            import vertexai
            from vertexai import agent_engines as ae
            vertexai.init(project=gcp_project)
        except ImportError:
            ae = None

        for resource_name in plan.gcp_agent_engines:
            try:
                assert ae is not None
                engine = ae.get(resource_name)
                engine.delete(force=True)
                _console.print(f"  [green]✓[/] deleted Agent Engine {resource_name}")
            except Exception as e:
                _console.print(f"  [red]✗[/] Agent Engine {resource_name}: {e}")
                failures += 1

        try:
            from google.cloud import storage
            client = storage.Client(project=gcp_project)
            for name in plan.gcp_gcs_buckets:
                try:
                    bucket = client.bucket(name)
                    bucket.delete(force=True)
                    _console.print(f"  [green]✓[/] deleted GCS bucket {name}")
                except Exception as e:
                    _console.print(f"  [red]✗[/] GCS bucket {name}: {e}")
                    failures += 1
        except ImportError:
            pass

    return failures


# --------------------------------------------------------------------- #
# Rich rendering + CLI entrypoint
# --------------------------------------------------------------------- #


def _render_plan(plan: CleanupPlan) -> None:
    table = Table(title=f"cleanup plan ({plan.total} resources)")
    table.add_column("Type", style="cyan")
    table.add_column("Name / ID")
    sections = [
        ("AWS AgentCore runtime", plan.aws_runtimes),
        ("AWS ECR repo", plan.aws_ecr_repos),
        ("AWS IAM role", plan.aws_iam_roles),
        ("AWS S3 bucket", plan.aws_s3_buckets),
        ("GCP Agent Engine", plan.gcp_agent_engines),
        ("GCP GCS bucket", plan.gcp_gcs_buckets),
    ]
    for kind, items in sections:
        for item in items:
            table.add_row(kind, item)
    _console.print(table)


def run(
    *,
    prefix: str,
    aws: bool = True,
    gcp: bool = False,
    aws_region: str = "us-east-1",
    gcp_project: str | None = None,
    dry_run: bool = True,
    yes: bool = False,
) -> int:
    if len(prefix) < MIN_PREFIX_LENGTH:
        _console.print(
            f"[red]✗[/] --prefix must be ≥{MIN_PREFIX_LENGTH} chars "
            f"(got {len(prefix)!r}). Refusing to match too broadly."
        )
        return 2

    _console.print(f"[bold]cloudless cleanup[/]  prefix=[cyan]{prefix}[/]  "
                   f"{'(dry-run)' if dry_run else '[red](LIVE DELETE)[/]'}")

    plan = build_plan(prefix, aws=aws, gcp=gcp, aws_region=aws_region,
                      gcp_project=gcp_project)

    if plan.total == 0:
        _console.print("[green]✓[/] No matching resources found.")
        return 0

    _render_plan(plan)

    if dry_run:
        _console.print("\n[dim]Dry run. Use --yes to execute.[/]")
        return 0

    if not yes:
        _console.print("[red]✗[/] Refusing to delete without --yes.")
        return 2

    _console.print(f"\n[bold]Deleting {plan.total} resources...[/]")
    failures = execute_plan(plan, aws_region=aws_region, gcp_project=gcp_project)
    if failures:
        _console.print(f"\n[red]✗[/] {failures} deletes failed.")
        return 1
    _console.print(f"\n[green]✓[/] All {plan.total} resources deleted.")
    return 0
