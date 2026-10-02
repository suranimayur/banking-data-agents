"""The serving stacks: the Copilot API, and the analyst console that calls it.

**The console has no business logic.** It authenticates a human with Cognito,
calls the API, and renders the evidence envelope the API returns. Every rule about
what may be asked, what may be answered and what must be refused lives behind the
API, so the console cannot be the place where governance is bypassed.

**The API holds no data credentials of the analyst.** It invokes agents through
their AgentCore endpoints with its own instance role; the analyst's identity
decides *whether* they may ask, the agent's role decides *what* may be read. That
separation is why an analyst cannot escalate by crafting a request.

**Both are App Runner services.** No load balancer, no cluster to patch, TLS and
scaling handled, and a scale-to-zero-and-back story that suits a platform used in
office hours. The console service is created with the API's URL as an environment
variable, so the wiring exists in CloudFormation rather than in someone's shell
history.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_apprunner as apprunner
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import apply_tags, grant_answer_store, grant_config_read, is_destroyable
from infra.stacks.identity import IdentityStack
from infra.stacks.storage_lake import StorageLakeStack

#: Actions the API's instance role needs to reach the agents. AgentCore runtime
#: invocation is not resource-scopable per-endpoint in a way that survives a
#: redeploy, so the role is written against the action and constrained by the
#: account boundary instead.
AGENT_INVOKE_ACTIONS: tuple[str, ...] = (
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock:InvokeModel",
    "bedrock:ApplyGuardrail",
)


class _ServingStack(Stack):
    """Shared App Runner plumbing for the API and the console.

    Both services differ in three values — image, port, health path — and are
    otherwise identical, so the difference is expressed as arguments rather than as
    two near-copies of the same fifty lines.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        name: str,
        port: int,
        health_check_path: str,
        image_tag: str,
        environment: dict[str, str],
        cpu: str = "1024",
        memory: str = "2048",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} {name} service ({config.name})",
            **kwargs,
        )
        self.config = config

        self.repository = ecr.Repository(
            self,
            "Repository",
            repository_name=qualify(name, config.name),
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            encryption_key=storage.key,
            lifecycle_rules=[ecr.LifecycleRule(description="keep the last 50 images", max_image_count=50)],
            removal_policy=config.removal_policy,
            empty_on_delete=is_destroyable(config),
        )

        # Two roles, deliberately. The *access* role lets App Runner pull the
        # image; the *instance* role is what the running container is. Merging them
        # would give the container pull rights it never uses.
        self.access_role = iam.Role(
            self,
            "AccessRole",
            role_name=qualify(f"{name}-apprunner-access", config.name),
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSAppRunnerServicePolicyForECRAccess")
            ],
        )
        self.instance_role = iam.Role(
            self,
            "InstanceRole",
            role_name=qualify(f"{name}-instance", config.name),
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )

        self.service = apprunner.CfnService(
            self,
            "Service",
            service_name=qualify(name, config.name),
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                # Deploys go through CDK and the pipeline; App Runner must not also
                # be able to deploy when someone pushes a tag.
                auto_deployments_enabled=False,
                authentication_configuration=apprunner.CfnService.AuthenticationConfigurationProperty(
                    access_role_arn=self.access_role.role_arn
                ),
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=f"{self.repository.repository_uri}:{image_tag}",
                    image_repository_type="ECR",
                    image_configuration=apprunner.CfnService.ImageConfigurationProperty(
                        port=str(port),
                        # CloudFormation models environment as a list of key/value
                        # pairs; the mapping passed in is only for readability.
                        runtime_environment_variables=[
                            apprunner.CfnService.KeyValuePairProperty(name=key, value=value)
                            for key, value in environment.items()
                        ],
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu=cpu,
                memory=memory,
                instance_role_arn=self.instance_role.role_arn,
            ),
            health_check_configuration=apprunner.CfnService.HealthCheckConfigurationProperty(
                protocol="HTTP",
                path=health_check_path,
                interval=10,
                timeout=5,
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
            tags=[{"key": "Environment", "value": config.name}],
        )

        apply_tags(self, config)
        CfnOutput(self, f"{name.title()}Url", value=self.service.attr_service_url)

    @property
    def service_url(self) -> str:
        return self.service.attr_service_url


class ApiStack(_ServingStack):
    """The Copilot HTTP API: ``/ask``, ``/products``, ``/quality``, ``/health``."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        identity: IdentityStack,
        image_tag: str = "latest",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            config=config,
            storage=storage,
            name="api",
            port=8000,
            health_check_path="/health",
            image_tag=image_tag,
            environment={
                "BDA_ENV": config.name,
                "BDA_GOLD_BUCKET": qualify("gold", config.name),
                "BDA_OPS_BUCKET": qualify("ops", config.name),
                "BDA_ARTIFACTS_BUCKET": qualify("artifacts", config.name),
                "ATHENA_WORKGROUP": qualify("agents", config.name),
                "ATHENA_BYTES_CAP_MB": str(config.athena_bytes_cap_mb),
                "AGENT_MAX_TOOL_CALLS": str(config.agent_max_tool_calls),
                "AGENT_MAX_TOKENS": str(config.agent_max_tokens),
                "BEDROCK_MODEL_ROUTER": config.model_router,
                "BEDROCK_MODEL_REASONER": config.model_reasoner,
                "BEDROCK_MODEL_ESCALATION": config.model_escalation,
                # Cognito values are ids, not secrets; they are read by the client.
                "BDA_USER_POOL_ID": identity.user_pool.user_pool_id,
                "BDA_USER_POOL_CLIENT_ID": identity.console_client.user_pool_client_id,
            },
            **kwargs,
        )

        self.instance_role.add_to_policy(
            # Not resource-scopable: AgentCore runtime ARNs are created per version and
            # a policy naming one would break on the next deploy. The account boundary
            # is the constraint here, and it is stated rather than implied.
            iam.PolicyStatement(
                actions=list(AGENT_INVOKE_ACTIONS),
                resources=["*"],
            )
        )

        # The API records every answered envelope for audit lookup and reads the
        # runtime configuration. Both grants are expressed as ARNs rather than by
        # calling ``grant_*`` on the storage constructs: a table grant also touches
        # the KMS key, and touching the key from here would make the storage stack
        # depend on this one for the role ARN — a dependency cycle.
        access = storage.lake_access()
        grant_answer_store(self.instance_role, access)
        grant_config_read(self.instance_role, access)


class ConsoleStack(_ServingStack):
    """The Streamlit analyst console: a thin client over the API."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        identity: IdentityStack,
        api_endpoint: str,
        image_tag: str = "latest",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            config=config,
            storage=storage,
            name="console",
            port=8501,
            # Streamlit's own health endpoint. Creating a bespoke /health route in
            # the UI would be a second thing to keep working.
            health_check_path="/_stcore/health",
            image_tag=image_tag,
            environment={
                "BDA_ENV": config.name,
                "BDA_API_URL": api_endpoint,
                "BDA_USER_POOL_ID": identity.user_pool.user_pool_id,
                "BDA_USER_POOL_CLIENT_ID": identity.console_client.user_pool_client_id,
            },
            # The console renders and proxies; it does not reason, so it does not
            # need the API's compute.
            cpu="512",
            memory="1024",
            **kwargs,
        )


__all__ = ["AGENT_INVOKE_ACTIONS", "ApiStack", "ConsoleStack"]
