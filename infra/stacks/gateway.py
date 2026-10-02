"""The gateway stack: one governed MCP endpoint, plus the Cedar policy engine.

Two problems are solved here that the application cannot solve for itself.

**Tool discovery and a single choke point.** The copilot's HTTP API is registered
as an MCP server target, so any MCP-capable client — the console, an IDE, a
colleague's notebook — reaches the governed surface through one endpoint that has
authentication, rate limiting and audit in one place. Without a gateway, every
consumer of the API is its own integration and its own security review.

**Policy that survives a bad prompt.** The application already refuses
prohibited use, and it does so by *policy*: ``tools/sqlguard.py`` refuses a query
against the ground-truth holdout, and the contract's ``not_allowed_use`` block
refuses marketing targeting on credit risk. This stack states the same rules a
second time, in Cedar, at the platform layer — because the first layer is code
that a future refactor can weaken, and the second is infrastructure that fails
closed. It is defence in depth, stated on purpose.

The Cedar statements are deliberately about *categories of action* rather than
tool names: "no agent may take an irreversible action" is a rule that keeps
holding when someone adds an eleventh tool.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.config import StageConfig, identifier, qualify
from infra.stacks.common import apply_tags
from infra.stacks.storage_lake import StorageLakeStack

#: Cedar requires qualified action ids (``Namespace::Action::"name"``) rather
#: than bare names, and the namespace is part of the policy's meaning: our tool
#: actions live under Bda, so a policy can never accidentally match an AWS-owned
#: action with the same short name.
ACTION_NAMESPACE = "Bda::Action"


#: Actions no agent may ever take, whatever its prompt says. These are the
#: irreversible ones: blocking a card, deciding credit, moving money, or rewriting
#: a customer record. The platform produces recommendations; humans execute.
IRREVERSIBLE_ACTIONS: tuple[str, ...] = (
    "execute_block",
    "finalize_decision",
    "disburse_funds",
    "write_off_loan",
    "update_customer_record",
    "delete_evidence",
)

#: Actions that reach outside the governed zones. The application cannot read
#: bronze or silver — no agent role has the IAM grant — and these actions are
#: forbidden here so that the attempt is denied at the gateway rather than
#: surfacing as a confusing AccessDenied from Athena.
RAW_ZONE_ACTIONS: tuple[str, ...] = (
    "read_bronze",
    "read_silver",
    "read_raw_pii",
    "query_lake_formation_holdout",
)

#: Per-agent prohibitions that encode a domain rule rather than a security rule.
#: The fraud agent investigates individuals; ranking customers for marketing by
#: risk is a use the credit data contract forbids, and a fraud agent has no
#: business doing it either.
AGENT_PROHIBITIONS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "fraud": (
        (
            "no_individual_ranking_for_marketing",
            (
                "rank_customers_by_credit_risk",
                "list_customers_for_marketing",
                "score_customers_for_targeting",
            ),
        ),
    ),
    "credit": (
        (
            "no_automated_decision",
            ("approve_credit", "decline_credit", "set_credit_limit"),
        ),
    ),
}


def _action(name: str) -> str:
    """Qualify a tool name as a Cedar action id."""
    return f'{ACTION_NAMESPACE}::"{name}"'


class GatewayStack(Stack):
    """An MCP endpoint in front of the platform, governed by a Cedar policy engine."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        api_endpoint: str,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} AgentCore gateway and policy ({config.name})",
            **kwargs,
        )
        self.config = config

        self.role = iam.Role(
            self,
            "GatewayRole",
            role_name=qualify("gateway", config.name),
            description="Role the AgentCore gateway assumes to invoke registered targets",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
        )

        self.gateway = agentcore.Gateway(
            self,
            "Gateway",
            # Awkward but true: the gateway and its targets take hyphens
            # (DNS-style names), while the policy engine takes underscores because
            # it doubles as a Cedar identifier. Both helpers are in use here.
            gateway_name=qualify("mcp", config.name),
            description=f"Governed MCP surface for {config.project} ({config.name})",
            role=self.role,
            kms_key=storage.key,
        )

        # The copilot API, registered as an MCP server target. ``credential
        # provider configurations`` are empty because the target is reached with the
        # gateway's own role: there is no third-party secret to manage here.
        self.api_target = self.gateway.add_mcp_server_target(
            "CopilotApi",
            credential_provider_configurations=[],
            endpoint=api_endpoint,
            description="Data Product Copilot: governed questions, evidence envelopes, refusals",
            gateway_target_name=qualify("copilot-api", config.name),
        )

        # -- policy engine ----------------------------------------------------
        self.policy_engine = agentcore.PolicyEngine(
            self,
            "PolicyEngine",
            policy_engine_name=identifier("policy", config.name),
            description=f"Cedar policy for agent tool use ({config.name})",
            kms_key=storage.key,
        )

        self.policies: dict[str, agentcore.Policy] = {
            "no_irreversible_actions": self.policy_engine.add_policy(
                "NoIrreversibleActions",
                policy_name="no_irreversible_actions",
                description="No agent may take an action that cannot be undone; humans execute.",
                statement=agentcore.PolicyStatement(
                    effect=agentcore.PolicyEffect.FORBID,
                    principal=agentcore.PolicyPrincipal.entity_type("Agent"),
                    action=agentcore.PolicyAction.any_of([_action(a) for a in IRREVERSIBLE_ACTIONS]),
                    resource=agentcore.PolicyResource.any_of_type("Tool"),
                ),
            ),
            "no_raw_zone_access": self.policy_engine.add_policy(
                "NoRawZoneAccess",
                policy_name="no_raw_zone_access",
                description="Agents read the governed gold zone, never bronze or silver.",
                statement=agentcore.PolicyStatement(
                    effect=agentcore.PolicyEffect.FORBID,
                    principal=agentcore.PolicyPrincipal.entity_type("Agent"),
                    action=agentcore.PolicyAction.any_of([_action(a) for a in RAW_ZONE_ACTIONS]),
                    resource=agentcore.PolicyResource.any_of_type("Tool"),
                ),
            ),
        }

        for agent_name, prohibitions in AGENT_PROHIBITIONS.items():
            for policy_name, actions in prohibitions:
                key = f"{agent_name}_{policy_name}"
                self.policies[key] = self.policy_engine.add_policy(
                    f"Policy{agent_name.title()}{policy_name.replace('_', ' ').title().replace(' ', '')}",
                    policy_name=policy_name,
                    description=f"{agent_name} agent must not perform: {', '.join(actions)}",
                    statement=agentcore.PolicyStatement(
                        effect=agentcore.PolicyEffect.FORBID,
                        principal=agentcore.PolicyPrincipal.entity("Agent", agent_name),
                        action=agentcore.PolicyAction.any_of([_action(a) for a in actions]),
                        resource=agentcore.PolicyResource.any_of_type("Tool"),
                    ),
                )

        # Who may call the gateway is not decided here. Invocation is granted by the
        # gateway's own resource policy, which is attached when a consumer is
        # registered — and consumers live in other stacks. Granting it here would
        # mean this stack referencing their roles, which is the dependency direction
        # that produces cycles.

        apply_tags(self, config)

        gateway_url = self.gateway.gateway_url
        if gateway_url is not None:
            CfnOutput(self, "GatewayUrl", value=gateway_url)
        CfnOutput(self, "GatewayId", value=self.gateway.gateway_id)
        CfnOutput(self, "PolicyEngineArn", value=self.policy_engine.policy_engine_arn)


__all__ = [
    "ACTION_NAMESPACE",
    "AGENT_PROHIBITIONS",
    "IRREVERSIBLE_ACTIONS",
    "RAW_ZONE_ACTIONS",
    "GatewayStack",
]
