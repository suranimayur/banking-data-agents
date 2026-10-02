"""The agent runtime stack: one hosted AgentCore runtime per agent.

**One runtime per agent, with its own role.** It would be cheaper and simpler to
host all three agents in one runtime behind one role. That would also mean the
fraud agent holds the credit agent's permissions, and "the fraud agent cannot read
the credit risk mart" would become a statement about code rather than about IAM.
Three runtimes, three roles, one grant helper.

**Two endpoints per runtime, always.** ``live`` takes production traffic and
``canary`` takes the candidate build. A deploy creates a *new runtime version* and
points the canary endpoint at it; promotion is a traffic switch, and rollback is
the same switch in the other direction. There is never an edit-in-place of the
version that is serving.

**Memory is a first-class resource, not a chat log.** The fraud analyst's running
case thread and the "always show me the 90-day baseline" preference are the two
things that make the second question cheap, and both belong in AgentCore Memory
with an explicit retention period rather than in a prompt.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.config import StageConfig, identifier, qualify
from infra.stacks.catalog import CatalogStack
from infra.stacks.common import (
    GOVERNED_ZONES,
    LakeAccess,
    apply_tags,
    grant_answer_store,
    grant_bedrock_invoke,
    grant_config_read,
    grant_governed_read,
    grant_lake_formation,
    is_destroyable,
)
from infra.stacks.guardrails import GuardrailsStack
from infra.stacks.storage_lake import StorageLakeStack

#: The agents this platform ships. Keys match ``bda.agents.registry.AGENTS`` and
#: the names used by ``bda ask``, the API and the evaluation suites.
AGENTS: dict[str, str] = {
    "copilot": "Governed data product question answering over the gold zone",
    "fraud": "Deterministic fraud triage: fetch, enrich, score, narrate, recommend",
    "credit": "Credit risk assessment that stops at a human sign-off gate",
}

#: Endpoint names. ``live`` is what the console and the API call; ``canary`` is
#: what a deploy writes to before promotion.
ENDPOINTS: tuple[str, ...] = ("live", "canary")

#: Long-term memory: how long a case thread survives. A year is the fraud
#: investigation window; beyond that the case is closed and read from the archive,
#: not from memory.
MEMORY_RETENTION_DAYS = 365


class AgentRuntimeStack(Stack):
    """ECR repositories, hosted runtimes, their endpoints, roles and memory."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        catalog: CatalogStack,
        guardrails: GuardrailsStack,
        image_tag: str = "latest",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} AgentCore runtimes ({config.name})",
            **kwargs,
        )
        self.config = config
        self.image_tag = image_tag

        access: LakeAccess = storage.lake_access()

        self.roles: dict[str, iam.Role] = {}
        self.repositories: dict[str, ecr.Repository] = {}
        self.runtimes: dict[str, agentcore.Runtime] = {}
        self.endpoints: dict[str, dict[str, agentcore.RuntimeEndpoint]] = {}

        for name, description in AGENTS.items():
            role = iam.Role(
                self,
                f"Role{name.title()}",
                role_name=qualify(f"agent-{name}", config.name),
                description=f"Execution role for the {name} agent ({config.name})",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                max_session_duration=Duration.hours(1),
            )
            grant_bedrock_invoke(role, region=self.region, account=self.account)

            # The single governed data grant. Bronze and silver are absent from it
            # by construction: no agent role is ever granted them. Lake Formation is
            # authorised alongside IAM because a Lake Formation-governed catalog
            # checks both.
            grant_governed_read(role, access, zones=GOVERNED_ZONES)
            grant_lake_formation(self, role, access, zones=GOVERNED_ZONES)

            grant_answer_store(role, access)
            grant_config_read(role, access)

            repository = ecr.Repository(
                self,
                f"Repository{name.title()}",
                repository_name=qualify(f"agent-{name}", config.name),
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                image_scan_on_push=True,
                encryption_key=storage.key,
                lifecycle_rules=[ecr.LifecycleRule(description="keep the last 50 images", max_image_count=50)],
                removal_policy=config.removal_policy,
                empty_on_delete=is_destroyable(config),
            )

            runtime = agentcore.Runtime(
                self,
                f"Runtime{name.title()}",
                # Underscores only: AgentCore uses the runtime name as a Cedar
                # entity identifier, so a hyphen is rejected at deploy time.
                runtime_name=identifier(f"agent_{name}", config.name),
                description=description,
                execution_role=role,
                agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_ecr_repository(repository, image_tag),
                # Configuration travels as environment, secrets travel as secrets.
                # The guardrail id lands here from the guardrails stack so that the
                # deployed agent cannot be pointed at "the latest" rule set.
                environment_variables={
                    "BDA_ENV": config.name,
                    "BDA_AGENT": name,
                    "BDA_GOLD_BUCKET": access.zone_buckets["gold"],
                    "BDA_OPS_BUCKET": access.zone_buckets["ops"],
                    "BDA_ARTIFACTS_BUCKET": access.artifacts_bucket,
                    "BDA_ANSWER_TABLE": access.answer_table,
                    "ATHENA_WORKGROUP": qualify("agents", config.name),
                    "ATHENA_BYTES_CAP_MB": str(config.athena_bytes_cap_mb),
                    "AGENT_MAX_TOOL_CALLS": str(config.agent_max_tool_calls),
                    "AGENT_MAX_TOKENS": str(config.agent_max_tokens),
                    "BEDROCK_MODEL_ROUTER": config.model_router,
                    "BEDROCK_MODEL_REASONER": config.model_reasoner,
                    "BEDROCK_MODEL_ESCALATION": config.model_escalation,
                    "BEDROCK_GUARDRAIL_ID": guardrails.guardrail.attr_guardrail_id,
                    "BEDROCK_GUARDRAIL_VERSION": guardrails.guardrail_version.attr_version,
                },
                tracing_enabled=True,
            )

            # The live endpoint pins a version when the stage says to (that is what a
            # promotion is), and otherwise follows whatever was deployed last. The
            # canary never pins: it is where the newly built version is proven, and a
            # pinned canary would be a canary that stops canarying.
            live_version = config.live_agent_versions.get(name)
            endpoints = {
                endpoint_name: agentcore.RuntimeEndpoint(
                    self,
                    f"Endpoint{name.title()}{endpoint_name.title()}",
                    agent_runtime_id=runtime.agent_runtime_id,
                    endpoint_name=endpoint_name,
                    agent_runtime_version=live_version if endpoint_name == "live" else None,
                    description=(
                        f"{name} {endpoint_name} endpoint ({config.name}, "
                        f"pinned to version {live_version})"
                        if endpoint_name == "live" and live_version
                        else f"{name} {endpoint_name} endpoint ({config.name})"
                    ),
                )
                for endpoint_name in ENDPOINTS
            }

            self.roles[name] = role
            self.repositories[name] = repository
            self.runtimes[name] = runtime
            self.endpoints[name] = endpoints

        # -- memory ----------------------------------------------------------
        # Shared across agents on purpose: a fraud investigation and a credit
        # assessment on the same customer should see the same case history, and
        # that is a feature of the domain, not a leak.
        self.memory = agentcore.Memory(
            self,
            "CaseMemory",
            memory_name=identifier("case_memory", config.name),
            description=f"Session threads and case history for the agents ({config.name})",
            expiration_duration=Duration.days(MEMORY_RETENTION_DAYS),
            kms_key=storage.key,
            memory_strategies=[
                agentcore.MemoryStrategy.using_built_in_semantic(),
                agentcore.MemoryStrategy.using_built_in_summarization(),
                agentcore.MemoryStrategy.using_built_in_user_preference(),
            ],
        )
        for role in self.roles.values():
            self.memory.grant_read(role)
            self.memory.grant_write(role)

        apply_tags(self, config)

        for name, runtime in self.runtimes.items():
            CfnOutput(self, f"RuntimeArn{name.title()}", value=runtime.agent_runtime_arn)
        CfnOutput(self, "MemoryArn", value=self.memory.memory_arn)


__all__ = ["AGENTS", "ENDPOINTS", "MEMORY_RETENTION_DAYS", "AgentRuntimeStack"]
