"""The lake storage stack: keys, buckets, the answer store and the runtime config.

This is the stack that has to be right, because everything else assumes it. The
decisions encoded here:

**One customer-managed KMS key for the lake.** Not ``aws/s3``. A bank needs to be
able to say who can decrypt its data and to revoke that without rekeying every
bucket separately, and it needs the key's own policy to be reviewable.

**Buckets are private and versioned.** ``BLOCK_ALL`` is not a setting to
negotiate. Versioning is on everywhere except dev, and non-current versions
expire, because the alternative to expiring them is paying for them forever.

**Athena results get their own bucket.** A result set is scratch, and mixing
scratch into the governed zone is how a table nobody owns appears in the catalog.

**The answer store is a table, not a log.** Envelopes are retained for
``answer_retention_days`` with TTL, so a question about a number from last quarter
has an answer, and the table does not grow without limit.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, SecretValue, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import LakeAccess, apply_tags, is_destroyable, log_retention

#: The medallion zones, in the order data flows through them.
ZONE_BUCKETS: dict[str, str] = {
    "bronze": "bronze",
    "silver": "silver",
    "gold": "gold",
    "ops": "ops",
}


class StorageLakeStack(Stack):
    """Encrypted, versioned lake zones plus the audited answer store."""

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} lake storage and answer store ({config.name})",
            **kwargs,
        )
        self.config = config

        # -- key -------------------------------------------------------------
        self.key = kms.Key(
            self,
            "LakeKey",
            alias=f"alias/{qualify('lake', config.name)}",
            description=f"{config.project} lake encryption key ({config.name})",
            enable_key_rotation=True,
            # Long enough that data encrypted under a retired key stays readable;
            # deletion is deliberately a slow, two-party decision.
            pending_window=Duration.days(30),
            removal_policy=config.removal_policy,
        )

        # -- lake zones ------------------------------------------------------
        self.buckets: dict[str, s3.Bucket] = {
            zone: self._zones(zone, slug, transition_days=config.bronze_transition_days if zone == "bronze" else None)
            for zone, slug in ZONE_BUCKETS.items()
        }

        # -- scratch ---------------------------------------------------------
        self.artifacts_bucket = self._zones("artifacts", "artifacts", versioned=False)

        # -- audited answer store --------------------------------------------
        self.answer_store = dynamodb.TableV2(
            self,
            "AnswerStore",
            table_name=qualify("answers", config.name),
            partition_key=dynamodb.Attribute(name="trace_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="asked_at", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            encryption=dynamodb.TableEncryptionV2.customer_managed_key(self.key),
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=config.name != "dev",
            ),
            removal_policy=config.removal_policy,
        )

        # -- runtime configuration -------------------------------------------
        # Configuration the agents read at runtime. No secrets in the image, and no
        # environment variable holding something that should be rotatable.
        self.config_secret = secretsmanager.Secret(
            self,
            "AgentConfig",
            secret_name=qualify("agent-config", config.name),
            description=f"{config.project} agent runtime configuration",
            encryption_key=self.key,
            secret_object_value={
                "athena_workgroup": SecretValue.unsafe_plain_text(qualify("agents", config.name)),
                "athena_bytes_cap_mb": SecretValue.unsafe_plain_text(str(config.athena_bytes_cap_mb)),
                "agent_max_tool_calls": SecretValue.unsafe_plain_text(str(config.agent_max_tool_calls)),
                "agent_max_tokens": SecretValue.unsafe_plain_text(str(config.agent_max_tokens)),
                "bedrock_model_router": SecretValue.unsafe_plain_text(config.model_router),
                "bedrock_model_reasoner": SecretValue.unsafe_plain_text(config.model_reasoner),
                "bedrock_model_escalation": SecretValue.unsafe_plain_text(config.model_escalation),
            },
            removal_policy=config.removal_policy,
        )

        # -- logs ------------------------------------------------------------
        self.agent_log_group = logs.LogGroup(
            self,
            "AgentLogs",
            log_group_name=f"/aws/bda/{config.name}/agents",
            retention=log_retention(config.log_retention_days),
            encryption_key=self.key,
            removal_policy=config.removal_policy,
        )

        apply_tags(self, config)

        CfnOutput(self, "GoldBucketName", value=self.buckets["gold"].bucket_name)
        CfnOutput(
            self,
            "ContractsAreVersionedInGit",
            value="contracts/ and semantic/ ship with the application, not with CloudFormation",
        )

    # -- helpers -------------------------------------------------------------
    def lake_access(self) -> LakeAccess:
        """Describe this lake as plain names and ARNs, for other stacks to grant against.

        The bucket and table names are deterministic, so they travel as strings and
        create no dependency. The KMS key ARN is generated by KMS and therefore a
        token, so it is the one value that does create a (one-directional)
        reference — which is the trade for a key whose policy stays reviewable.
        """
        return LakeAccess(
            region=self.region,
            account=self.account,
            zone_buckets={zone: bucket.bucket_name for zone, bucket in self.buckets.items()},
            artifacts_bucket=self.artifacts_bucket.bucket_name,
            key_arn=self.key.key_arn,
            workgroup_arn=f"arn:aws:athena:{self.region}:{self.account}:workgroup/{qualify('agents', self.config.name)}",
            databases={zone: qualify(f"{zone}_db", self.config.name) for zone in ZONE_BUCKETS},
            answer_table=self.answer_store.table_name,
            config_secret_name=qualify("agent-config", self.config.name),
        )

    def _zones(self, zone: str, slug: str, *, versioned: bool = True, transition_days: int | None = None) -> s3.Bucket:
        """Create one lake bucket with the lifecycle rules its role deserves."""
        rules: list[s3.LifecycleRule] = []

        if transition_days is not None:
            rules.append(
                s3.LifecycleRule(
                    id="archive-cold-bronze",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(transition_days),
                        )
                    ],
                )
            )
        if versioned and self.config.noncurrent_version_expiry_days is not None:
            rules.append(
                s3.LifecycleRule(
                    id="expire-noncurrent-versions",
                    enabled=True,
                    noncurrent_version_expiration=Duration.days(self.config.noncurrent_version_expiry_days),
                )
            )
        if not versioned:
            rules.append(s3.LifecycleRule(id="expire-scratch", enabled=True, expiration=Duration.days(30)))

        return s3.Bucket(
            self,
            f"{zone.title()}Bucket",
            bucket_name=qualify(slug, self.config.name),
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            versioned=versioned,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # Rejects any request not made over TLS, including from inside the VPC.
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=rules or None,
            removal_policy=self.config.removal_policy,
            # Only dev may delete a non-empty bucket; everywhere else the stack
            # fails to delete rather than destroying a lake.
            auto_delete_objects=is_destroyable(self.config),
        )


__all__ = ["ZONE_BUCKETS", "StorageLakeStack"]
