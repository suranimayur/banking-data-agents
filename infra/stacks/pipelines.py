"""The pipelines stack: the medallion run, orchestrated and scheduled.

The pipeline is the *same container image* that hosts the agents, run with a
different entrypoint (``bda pipeline run --stages ...``). That is a deliberate
choice over the more common "reimplement the transforms as Glue PySpark jobs":
there is one implementation of bronze→silver→gold, it is the one the test suite
exercises, and it is the one that runs on a laptop against Floci. A parallel
PySpark implementation would be a second source of truth that drifts.

What this stack adds, then, is orchestration and the operational scaffolding
around that image:

**A state machine, not a cron'd script.** Each medallion stage is its own state
with its own retry policy, so a transient S3 error retries silver without
re-running generation, and a failure stops the run instead of publishing a
half-built gold zone.

**The schedule is data, not a cron string in a workflow.** EventBridge owns the
trigger, so the nightly run exists even when GitHub is unavailable.

**A feature store and a stream.** Gold features are exported to DynamoDB so the
online path is a single-digit-millisecond read, and card transactions arrive on
Kinesis for the fraud agent to evaluate as they land rather than overnight.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesis as kinesis
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.catalog import CatalogStack
from infra.stacks.common import (
    LakeAccess,
    apply_tags,
    grant_config_read,
    grant_lake_writer,
    is_destroyable,
    log_retention,
)
from infra.stacks.network import NetworkStack
from infra.stacks.storage_lake import ZONE_BUCKETS, StorageLakeStack

#: The medallion stages the state machine runs, in order, with the CLI arguments
#: each one needs. Kept as data so the state machine mirrors the Makefile targets
#: instead of inventing a second vocabulary for the same steps.
STAGES: tuple[tuple[str, str, list[str]], ...] = (
    ("Generate", "Synthetic source data", ["bda", "data", "generate"]),
    ("Bronze", "Raw ingestion into the bronze zone", ["bda", "pipeline", "run", "--stages", "bronze"]),
    ("Silver", "Cleanse, type and conform into silver", ["bda", "pipeline", "run", "--stages", "silver"]),
    ("Gold", "Business marts into the gold zone", ["bda", "pipeline", "run", "--stages", "gold"]),
    ("Publish", "Contracts, metrics and lineage publication", ["bda", "pipeline", "run", "--stages", "publish"]),
    ("Quality", "Data quality rules and the DQ gate", ["bda", "pipeline", "run", "--stages", "dq"]),
)

#: Nightly, in UTC. Off the hour so the run does not collide with every other
#: cron job in the account.
NIGHTLY_CRON: events.Schedule = events.Schedule.cron(minute="17", hour="2")


class PipelinesStack(Stack):
    """ECS Fargate task definitions, a Step Functions run, and the triggers."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        network: NetworkStack,
        storage: StorageLakeStack,
        catalog: CatalogStack,
        image_tag: str = "latest",
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} medallion pipeline orchestration ({config.name})",
            **kwargs,
        )
        self.config = config
        self.image_tag = image_tag

        # The pipeline owns its role: nothing outside this stack can widen it, and
        # the pipeline does not have to reach into another stack to grant itself a
        # permission.
        self._pipeline_role = iam.Role(
            self,
            "PipelineRole",
            role_name=qualify("pipeline", config.name),
            description="Role for the medallion pipeline runs",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # -- image ------------------------------------------------------------
        self.repository = ecr.Repository(
            self,
            "Repository",
            repository_name=qualify("pipeline", config.name),
            # A tag is a promise. Immutability is what stops the promise being
            # rewritten under a running environment.
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            image_scan_on_push=True,
            encryption_key=storage.key,
            lifecycle_rules=[
                ecr.LifecycleRule(description="keep the last 30 images", max_image_count=30),
            ],
            removal_policy=config.removal_policy,
            empty_on_delete=is_destroyable(config),
        )

        # -- cluster ----------------------------------------------------------
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=qualify("pipeline", config.name),
            vpc=network.vpc,
            # Container Insights is what turns "the run was slow" into "the gold
            # stage was CPU-bound"; it costs a little, and pays for itself the
            # first time a nightly run overruns.
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )
        self.security_group = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=network.vpc,
            security_group_name=qualify("pipeline-tasks", config.name),
            description="Egress-only security group for pipeline tasks",
            allow_all_outbound=False,
        )
        # The VPC is isolated, so "outbound" here means "to the VPC endpoints".
        self.security_group.add_egress_rule(
            peer=ec2.Peer.ipv4(network.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp_range(443, 443),
            description="HTTPS to the VPC interface endpoints (S3, Glue, Athena, KMS, ECR)",
        )

        self.log_group = logs.LogGroup(
            self,
            "PipelineLogs",
            log_group_name=f"/aws/bda/{config.name}/pipeline",
            retention=log_retention(config.log_retention_days),
            encryption_key=storage.key,
            removal_policy=config.removal_policy,
        )

        # One task definition per stage, rather than one task definition with a
        # container command overridden per state. Overrides are passed through
        # Step Functions input path plumbing that has to be kept in sync by hand;
        # a task definition per stage is more resources on paper and none in
        # practice, and the state machine reads as "run the bronze task".
        self.task_definitions: dict[str, ecs.FargateTaskDefinition] = {
            name: self._task_definition(name, command) for name, _comment, command in STAGES
        }

        self.task_definition = self.task_definitions[STAGES[0][0]]

        # -- nightly stream and feature store ---------------------------------
        self.transaction_stream = kinesis.Stream(
            self,
            "TransactionStream",
            stream_name=qualify("transactions", config.name),
            shard_count=1,
            retention_period=Duration.hours(24),
            encryption=kinesis.StreamEncryption.KMS,
            encryption_key=storage.key,
        )
        self.feature_store = dynamodb.TableV2(
            self,
            "FeatureStore",
            table_name=qualify("features", config.name),
            partition_key=dynamodb.Attribute(name="customer_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="feature_name", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(storage.key),
            removal_policy=config.removal_policy,
        )

        # Grants come last: the task role needs the buckets, the catalog and the
        # feature store to all exist before its policy can name them.
        self._grant_pipeline_access(storage, catalog)

        # -- orchestration ----------------------------------------------------
        self.failure_topic = sns.Topic(
            self,
            "PipelineFailures",
            topic_name=qualify("pipeline-failures", config.name),
            display_name=f"BDA pipeline failures ({config.name})",
            master_key=storage.key,
        )

        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=qualify("pipeline", config.name),
            state_machine_type=sfn.StateMachineType.STANDARD,
            # A nightly run that has not finished in two hours is a failure, not a
            # slow success: the next run is due, and two overlapping runs writing
            # the same gold partitions is worse than one failed run.
            timeout=Duration.hours(2),
            definition_body=sfn.DefinitionBody.from_chainable(self._build_definition()),
            logs=sfn.LogOptions(
                destination=self.log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            tracing_enabled=True,
        )

        events.Rule(
            self,
            "NightlySchedule",
            rule_name=qualify("pipeline-nightly", config.name),
            description="Nightly medallion run; exists independently of CI",
            schedule=NIGHTLY_CRON,
            enabled=config.name != "dev",
            targets=[targets.SfnStateMachine(self.state_machine)],
        )

        apply_tags(self, config)

        CfnOutput(self, "PipelineStateMachineArn", value=self.state_machine.state_machine_arn)
        CfnOutput(self, "PipelineRepositoryUri", value=self.repository.repository_uri)
        CfnOutput(self, "FeatureStoreTable", value=self.feature_store.table_name)

    # -- helpers -------------------------------------------------------------
    def _grant_pipeline_access(self, storage: StorageLakeStack, catalog: CatalogStack) -> None:
        """Give the pipeline role exactly the access a medallion run needs."""
        role = self._pipeline_role
        access: LakeAccess = storage.lake_access()
        grant_lake_writer(role, access, zones=tuple(ZONE_BUCKETS))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                resources=[access.artifacts_bucket_arn, access.object_arn(access.artifacts_bucket)],
            )
        )
        grant_config_read(role, access)

        # The catalog is the pipeline's output surface as well as its input: bronze
        # creates tables, gold registers the products, ops records quality results.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetDatabase",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                    "glue:CreateDatabase",
                    "glue:CreateTable",
                    "glue:UpdateTable",
                    "glue:BatchCreatePartition",
                    "glue:BatchDeletePartition",
                ],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:database/*",
                    f"arn:aws:glue:{self.region}:{self.account}:table/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:StopQueryExecution",
                    "athena:GetWorkGroup",
                ],
                resources=[catalog.workgroup_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(actions=["athena:GetWorkGroup", "athena:ListWorkGroups"], resources=["*"])
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kinesis:PutRecord", "kinesis:PutRecords", "kinesis:DescribeStream"],
                resources=["*"],
            )
        )
        self.grant_feature_store(role)

    def grant_feature_store(self, role: iam.Role) -> None:
        """Grant feature-store read/write to a role.

        A method rather than an inline call because the fraud agent's online path
        needs the same grant, and one statement describing it beats two that drift.
        """
        self.feature_store.grant_read_write_data(role)

    def _task_definition(self, stage_name: str, command: list[str]) -> ecs.FargateTaskDefinition:
        """A Fargate task that runs one pipeline stage.

        The image is the same for every stage — the agent image, run with a
        different entrypoint — so there is one implementation of the transforms and
        one thing to test.
        """
        task_definition = ecs.FargateTaskDefinition(
            self,
            f"TaskDefinition{stage_name}",
            family=qualify(f"pipeline-{stage_name.lower()}", self.config.name),
            cpu=2048,
            memory_limit_mib=8192,
            task_role=self.pipeline_role_for(stage_name),
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_definition.add_container(
            "Pipeline",
            image=ecs.ContainerImage.from_ecr_repository(self.repository, self.image_tag),
            # Read-only root filesystem: a data job has no business writing to its
            # own container, and this makes a compromised job unable to persist.
            readonly_root_filesystem=True,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="pipeline", log_group=self.log_group),
            command=command,
            environment={
                "BDA_ENV": self.config.name,
                "AWS_REGION": self.config.region,
                # Talks to real AWS; the emulator is a laptop concern only.
                "LLM_PROVIDER": "bedrock",
            },
        )
        return task_definition

    def pipeline_role_for(self, stage_name: str) -> iam.Role:
        """The role a stage's task runs as.

        One role for every stage today. It is a method so that splitting the roles
        later — generation writing only to bronze, publication writing only to
        ops — is a change in one place rather than in six task definitions.
        """
        del stage_name  # deliberately shared until a stage needs its own
        return self._pipeline_role

    def _build_definition(self) -> sfn.IChainable:
        """Chain the medallion stages, with retries and a failure notification.

        Each stage runs its task synchronously (``RUN_JOB``) so the state machine's
        own history shows the stage that failed, and so a crashed container is a
        failed state rather than a fire-and-forget.
        """
        # Typed as the concrete state class, not ``sfn.State``: ``add_catch`` lives on
        # the task-state base, and mypy is right to point that out.
        states: list[tasks.EcsRunTask] = []
        for name, comment, _command in STAGES:
            state = tasks.EcsRunTask(
                self,
                f"Stage{name}",
                comment=comment,
                cluster=self.cluster,
                task_definition=self.task_definitions[name],
                launch_target=tasks.EcsFargateLaunchTarget(platform_version=ecs.FargatePlatformVersion.LATEST),
                integration_pattern=sfn.IntegrationPattern.RUN_JOB,
                subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                security_groups=[self.security_group],
                assign_public_ip=False,
                # Two retries with exponential backoff: S3 and Glue return transient
                # errors, and a nightly run that fails on one is a run nobody trusts.
                result_path=sfn.JsonPath.DISCARD,
            )
            state.add_retry(backoff_rate=2, interval=Duration.seconds(30), max_attempts=2)
            states.append(state)

        notify = tasks.SnsPublish(
            self,
            "NotifyFailure",
            topic=self.failure_topic,
            subject=f"BDA pipeline failed ({self.config.name})",
            message=sfn.TaskInput.from_json_path_at("$$"),
        )
        failed = sfn.Fail(
            self,
            "PipelineFailed",
            error="MedallionPipelineFailed",
            cause="A medallion stage failed after its retries; gold was not published.",
        )
        chain: sfn.Chain = sfn.Chain.start(states[0])
        for state in states[1:]:
            chain = chain.next(state)

        # The catch goes on the last state, not on the chain: ``Chain`` is a
        # composition helper and cannot own a catch of its own.
        states[-1].add_catch(notify.next(failed), errors=["States.ALL"], result_path="$.error")
        return chain


__all__ = ["NIGHTLY_CRON", "STAGES", "PipelinesStack"]
