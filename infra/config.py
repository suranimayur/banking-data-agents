"""Environment configuration.

One place where the difference between ``dev``, ``staging`` and ``prod`` is
stated. Everything a stack needs to behave differently per environment — retention,
destruction policy, capacity, alarm thresholds, model tiers, budget — is a field
here, so a stack never contains an ``if env == "prod"``.

The defaults are deliberately conservative on the two things that are expensive to
get wrong: what gets deleted, and how loudly a problem is announced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from aws_cdk import RemovalPolicy


@dataclass(frozen=True)
class StageConfig:
    """Everything that varies between environments."""

    name: str
    account: str | None
    region: str = "us-east-1"
    #: How the stack treats resources it owns when the stack is deleted.
    removal_policy: RemovalPolicy = RemovalPolicy.RETAIN
    #: Version the data buckets. Costs storage; buys the ability to recover.
    version_data_buckets: bool = True
    #: Days after which non-current object versions are expired. ``None`` keeps them.
    noncurrent_version_expiry_days: int | None = 30
    #: Days after which bronze objects move to infrequent access. ``None`` disables.
    bronze_transition_days: int | None = None
    log_retention_days: int = 30
    #: Athena's per-query scan ceiling. The guardrail's real cost control.
    athena_bytes_cap_mb: int = 512
    #: Concurrent agent invocations the runtime will accept per account.
    agent_concurrency: int = 4
    #: Per-invocation agent budget, mirrored from the application settings.
    agent_max_tool_calls: int = 8
    agent_max_tokens: int = 60_000
    #: Agent runtime versions the ``live`` endpoint should serve, per agent.
    #:
    #: Empty (the default) means "follow the newest deployed version", which is how
    #: dev behaves and what makes a first deploy work without ceremony. A non-empty
    #: map is a *promotion*: the ``live`` endpoint is pinned to the exact version the
    #: canary served, so that deploying the next version does not move production
    #: traffic, and rolling back is re-pinning to the previous entry.
    live_agent_versions: dict[str, str] = field(default_factory=dict)
    #: Model tiers. Router classifies, reasoner answers, escalation root-causes.
    model_router: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    model_reasoner: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    model_escalation: str = "anthropic.claude-3-opus-20240229-v1:0"
    #: Monthly spend ceiling. A cost incident is an incident.
    monthly_budget_usd: float = 250.0
    #: Where alarms and budget notifications go.
    alarm_email: str | None = None
    #: Redirect URLs the console's Cognito client may use for a hosted-UI OAuth
    #: flow. Empty (the default) means the console authenticates with SRP and the
    #: client has no OAuth settings at all.
    console_callback_urls: tuple[str, ...] = ()
    #: DQ rules failing at this severity page someone. Below it, a dashboard.
    alarm_on_dq_warnings: bool = False
    #: How long answered envelopes are retained for audit lookup.
    answer_retention_days: int = 90
    #: Table names come from the app's settings, so this mirrors them for the
    #: pipeline's environment. Kept explicit so the stack is readable.
    project: str = "banking-data-agents"
    tags: dict[str, str] = field(default_factory=dict)


STAGES: dict[str, StageConfig] = {
    "dev": StageConfig(
        name="dev",
        account=None,  # resolved from the CDK environment at synth time
        # A developer environment is meant to be torn down and rebuilt.
        removal_policy=RemovalPolicy.DESTROY,
        version_data_buckets=False,
        noncurrent_version_expiry_days=None,
        log_retention_days=7,
        athena_bytes_cap_mb=256,
        agent_concurrency=2,
        monthly_budget_usd=50.0,
        answer_retention_days=7,
        tags={"CostCentre": "platform-dev"},
    ),
    "staging": StageConfig(
        name="staging",
        account=None,
        removal_policy=RemovalPolicy.RETAIN,
        log_retention_days=14,
        athena_bytes_cap_mb=512,
        agent_concurrency=4,
        monthly_budget_usd=150.0,
        answer_retention_days=30,
        tags={"CostCentre": "platform-staging"},
    ),
    "prod": StageConfig(
        name="prod",
        account=None,
        region="us-east-1",
        removal_policy=RemovalPolicy.RETAIN,
        version_data_buckets=True,
        noncurrent_version_expiry_days=90,
        bronze_transition_days=90,
        log_retention_days=400,
        athena_bytes_cap_mb=1024,
        agent_concurrency=12,
        agent_max_tool_calls=8,
        monthly_budget_usd=1500.0,
        # Warnings are visible in prod but do not page; only criticals do.
        alarm_on_dq_warnings=False,
        answer_retention_days=365,
        tags={"CostCentre": "risk-analytics", "DataClassification": "confidential"},
    ),
}

#: Environments the CLI accepts. ``local`` is not a deployed environment — it is
#: Floci on a laptop, and it is handled by the application, not by CloudFormation.
DEPLOYABLE = tuple(STAGES)


@dataclass(frozen=True)
class BootstrapConfig:
    """Inputs for the one-time, per-account bootstrap stack.

    These are properties of the *account and the repository*, not of an
    environment, which is why they are not fields on :class:`StageConfig`. The
    OIDC trust policy, for instance, is written once and covers every environment,
    differing only in the ``sub`` condition it accepts.
    """

    github_owner: str = "your-org"
    github_repo: str = "banking-data-agents"
    region: str = "us-east-1"
    #: Branches allowed to deploy. Everything else has to go through a pull request.
    protected_branches: tuple[str, ...] = ("main",)
    #: Environments that get an OIDC deploy role. Mirrors the deployable stages.
    environments: tuple[str, ...] = ("dev", "staging", "prod")
    #: Days a non-current object version in the state bucket is kept. State that
    #: cannot be rolled back is state that cannot be recovered from a bad deploy.
    state_noncurrent_days: int = 90

    @property
    def repository(self) -> str:
        """``owner/repo``, as GitHub writes it in an OIDC subject claim."""
        return f"{self.github_owner}/{self.github_repo}"

    @property
    def is_placeholder(self) -> bool:
        """True when the owner/repo still hold the shipped defaults."""
        return self.github_owner == "your-org"


#: Environment variable carrying a promotion decision to the deploy.
LIVE_VERSION_ENV_VAR = "BDA_LIVE_AGENT_VERSIONS"


def parse_agent_versions(raw: str | None) -> dict[str, str]:
    """Parse ``copilot=12,fraud=9`` into ``{"copilot": "12", "fraud": "9"}``.

    A deliberately small format rather than JSON: this value is typed on a command
    line during an incident, and during an incident the simplest thing that works is
    the right thing.
    """
    versions: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"expected '<agent>=<version>' but got {chunk!r}")
        agent, version = chunk.split("=", 1)
        agent, version = agent.strip(), version.strip()
        if not agent or not version:
            raise ValueError(f"expected '<agent>=<version>' but got {chunk!r}")
        versions[agent] = version
    return versions


def live_agent_versions() -> dict[str, str]:
    """The live-endpoint pin, read from the environment."""
    return parse_agent_versions(os.environ.get(LIVE_VERSION_ENV_VAR))


def bootstrap_config() -> BootstrapConfig:
    """Resolve the bootstrap configuration from the environment.

    Environment variables rather than a committed literal, so that a fork does not
    have to edit a file to be deployable, and so that the trust condition cannot be
    left pointing at somebody else's repository by accident.
    """
    return BootstrapConfig(
        github_owner=os.environ.get("BDA_GITHUB_OWNER", BootstrapConfig.github_owner),
        github_repo=os.environ.get("BDA_GITHUB_REPO", BootstrapConfig.github_repo),
        region=os.environ.get("BDA_REGION", BootstrapConfig.region),
    )


def stage(name: str) -> StageConfig:
    """Resolve an environment name to its configuration."""
    key = name.strip().lower()
    if key not in STAGES:
        raise KeyError(f"unknown environment '{name}'. Available: {', '.join(DEPLOYABLE)}")
    return STAGES[key]


def identifier(name: str, env: str) -> str:
    """Environment-qualified name for the APIs that reject hyphens.

    AgentCore runtime and memory names accept only letters, digits and
    underscores, because they are used as Cedar entity identifiers. This is the
    same naming decision as :func:`qualify` with the separator changed, kept as a
    named function so that "why is this one different" has an answer.
    """
    return f"bda_{env}_{name}".replace("-", "_")


def qualify(name: str, env: str) -> str:
    """Prefix a physical resource name with the environment.

    Physical names carry the environment because a resource name is what shows up
    in a bill, a log line and an incident channel; ``bda-bronze`` in three accounts
    is indistinguishable in a screenshot.
    """
    return f"bda-{env}-{name}"


__all__ = [
    "DEPLOYABLE",
    "LIVE_VERSION_ENV_VAR",
    "STAGES",
    "BootstrapConfig",
    "StageConfig",
    "bootstrap_config",
    "identifier",
    "live_agent_versions",
    "parse_agent_versions",
    "qualify",
    "stage",
]
