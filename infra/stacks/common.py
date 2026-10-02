"""Helpers shared by every stack.

Two things live here, and only two, because they are the two things every stack
must do *identically*: turn a retention day count into a CloudWatch rung, and
grant the read access the whole governance story rests on.

**Why the grants take plain names and ARNs rather than constructs.** It is the
natural thing to write ``bucket.grant_read(role)``, and it is the thing that
quietly welds two stacks together: the grant creates a bucket policy *in the bucket
owner's stack* that references a role owned by the caller, and the caller's stack
already references the bucket. The result is a CloudFormation dependency cycle the
day someone adds one more grant. So the grants here are expressed as IAM policy
statements on the *caller's own role*, built from values that carry no construct
tokens (plus the KMS key ARN, which flows one way only).

The upside beyond avoiding the cycle: the exact permissions an agent role receives
are written down in one place, in one idiom, and can be read as a sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aws_cdk import RemovalPolicy, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lakeformation as lakeformation
from aws_cdk import aws_logs as logs
from constructs import Construct

if TYPE_CHECKING:  # pragma: no cover - avoids an import cycle for type checkers only
    from infra.config import StageConfig

#: Ascending retention rungs. The first entry whose limit is >= the request wins.
_RETENTION_LADDER: tuple[tuple[int, logs.RetentionDays], ...] = (
    (1, logs.RetentionDays.ONE_DAY),
    (3, logs.RetentionDays.THREE_DAYS),
    (7, logs.RetentionDays.ONE_WEEK),
    (14, logs.RetentionDays.TWO_WEEKS),
    (30, logs.RetentionDays.ONE_MONTH),
    (60, logs.RetentionDays.TWO_MONTHS),
    (90, logs.RetentionDays.THREE_MONTHS),
    (120, logs.RetentionDays.FOUR_MONTHS),
    (150, logs.RetentionDays.FIVE_MONTHS),
    (180, logs.RetentionDays.SIX_MONTHS),
    (365, logs.RetentionDays.ONE_YEAR),
    (400, logs.RetentionDays.THIRTEEN_MONTHS),
    (545, logs.RetentionDays.EIGHTEEN_MONTHS),
    (731, logs.RetentionDays.TWO_YEARS),
)

#: Zones an agent principal is ever allowed to read: the published products, and the
#: quality/lineage metadata that explains them. Bronze and silver are absent, which
#: is the mechanism behind "the agent cannot see raw PII".
GOVERNED_ZONES: tuple[str, ...] = ("gold", "ops")

#: Model access every agent role needs. Written once, applied to each role in the
#: stack that owns it.
AGENT_MODEL_ACTIONS: tuple[str, ...] = (
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
    "bedrock:ApplyGuardrail",
)


@dataclass(frozen=True)
class LakeAccess:
    """Physical names and ARNs of the lake, as plain values.

    Produced by :meth:`infra.stacks.storage_lake.StorageLakeStack.lake_access` and
    consumed by whichever stack is granting access. Passing this instead of the
    storage stack is what keeps the dependency graph acyclic and the permissions
    reviewable.
    """

    region: str
    account: str
    zone_buckets: dict[str, str]
    artifacts_bucket: str
    key_arn: str
    workgroup_arn: str
    databases: dict[str, str]
    answer_table: str
    config_secret_name: str

    def bucket_arn(self, zone: str) -> str:
        """ARN of a lake zone bucket."""
        return f"arn:aws:s3:::{self.zone_buckets[zone]}"

    @property
    def artifacts_bucket_arn(self) -> str:
        return f"arn:aws:s3:::{self.artifacts_bucket}"

    @property
    def answer_table_arn(self) -> str:
        return f"arn:aws:dynamodb:{self.region}:{self.account}:table/{self.answer_table}"

    def object_arn(self, bucket: str) -> str:
        return f"arn:aws:s3:::{bucket}/*"


def log_retention(days: int) -> logs.RetentionDays:
    """Map a requested day count onto the nearest CDK retention rung.

    Rounds *up*, never down: a retention policy that silently keeps less than it
    was asked to keep is the wrong way to be wrong.
    """
    for limit, value in _RETENTION_LADDER:
        if days <= limit:
            return value
    return logs.RetentionDays.TEN_YEARS


def apply_tags(scope: Construct, config: StageConfig) -> None:
    """Apply the project, environment and cost-centre tags to everything in scope."""
    tags = {
        "Project": config.project,
        "Environment": config.name,
        "ManagedBy": "cdk",
        **config.tags,
    }
    for key, value in tags.items():
        Tags.of(scope).add(key, value)


def is_destroyable(config: StageConfig) -> bool:
    """True when a stack is allowed to delete its own data.

    Only ``dev`` says yes. Everywhere else a stack deletion is meant to be
    survivable, which is why this is a named predicate rather than an inline
    comparison on the enum.
    """
    return config.removal_policy == RemovalPolicy.DESTROY


def grant_bedrock_invoke(role: iam.Role, *, region: str, account: str) -> None:
    """Let a principal call models and apply the guardrail, and nothing else."""
    role.add_to_policy(
        iam.PolicyStatement(
            actions=list(AGENT_MODEL_ACTIONS),
            resources=[
                f"arn:aws:bedrock:{region}::foundation-model/*",
                f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                f"arn:aws:bedrock:{region}:{account}:guardrail/*",
            ],
        )
    )


def grant_governed_read(role: iam.Role, access: LakeAccess, *, zones: tuple[str, ...] = GOVERNED_ZONES) -> None:
    """Give a role read access to the governed zones, the catalog and the workgroup.

    This is the only path by which an agent principal receives data access. It
    covers the gold and ops zones — the published products and their quality and
    lineage metadata — and nothing else.
    """
    for zone in zones:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[access.object_arn(access.zone_buckets[zone])],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:GetBucketLocation"],
                resources=[access.bucket_arn(zone)],
            )
        )

    # Decrypt, but never encrypt: an agent role has no business writing to the lake
    # except through the answer store, which is a different grant.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["kms:Decrypt", "kms:DescribeKey"],
            resources=[access.key_arn],
        )
    )

    glue_resources: list[str] = [f"arn:aws:glue:{access.region}:{access.account}:catalog"]
    for database in access.databases.values():
        glue_resources.append(f"arn:aws:glue:{access.region}:{access.account}:database/{database}")
        glue_resources.append(f"arn:aws:glue:{access.region}:{access.account}:table/{database}/*")
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"],
            resources=glue_resources,
        )
    )

    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "athena:StartQueryExecution",
                "athena:GetQueryExecution",
                "athena:GetQueryResults",
                "athena:StopQueryExecution",
            ],
            resources=[access.workgroup_arn],
        )
    )
    # GetWorkGroup is not resource-scopable; it is the call that lets the client
    # learn the byte cap it must not exceed.
    role.add_to_policy(iam.PolicyStatement(actions=["athena:GetWorkGroup", "athena:ListWorkGroups"], resources=["*"]))

    # Athena writes its results into the artifacts bucket under the caller's prefix.
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
            resources=[access.object_arn(access.artifacts_bucket)],
        )
    )


def grant_answer_store(role: iam.Role, access: LakeAccess) -> None:
    """Let a role record and read evidence envelopes."""
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
            resources=[access.answer_table_arn, f"{access.answer_table_arn}/index/*"],
        )
    )


def grant_config_read(role: iam.Role, access: LakeAccess) -> None:
    """Let a role read the runtime configuration secret.

    The ARN carries a random suffix, so it is matched by prefix rather than by the
    full name; scoping to the name is still a narrowing compared with ``*``.
    """
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[f"arn:aws:secretsmanager:{access.region}:{access.account}:secret:{access.config_secret_name}-*"],
        )
    )


def grant_lake_writer(role: iam.Role, access: LakeAccess, *, zones: tuple[str, ...]) -> None:
    """Give a role read/write on whole lake zones.

    Used by the pipeline, which owns the lake contents. Deliberately *not* used by
    any agent role: an agent reads gold and writes evidence, never the other way
    round.
    """
    for zone in zones:
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"],
                resources=[access.object_arn(access.zone_buckets[zone])],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket", "s3:GetBucketLocation"],
                resources=[access.bucket_arn(zone)],
            )
        )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
            resources=[access.key_arn],
        )
    )


def grant_lake_formation(
    scope: Construct,
    role: iam.Role,
    access: LakeAccess,
    *,
    zones: tuple[str, ...] = GOVERNED_ZONES,
) -> None:
    """Grant database-level Lake Formation access inside the caller's own stack.

    IAM alone is not enough on a Lake Formation-governed catalog: a query is
    authorised twice, once by IAM and once by Lake Formation. Creating these in the
    caller's stack — rather than in the catalog stack, which would then have to
    reference the role — is what keeps the dependency graph one-directional.
    """
    for zone in zones:
        database = access.databases[zone]
        # Construct id carries the role, because several roles each need their own
        # permission on the same database and the ids must be unique within a stack.
        lakeformation.CfnPermissions(
            scope,
            f"LakeFormation{zone.title()}{role.node.id}",
            data_lake_principal=lakeformation.CfnPermissions.DataLakePrincipalProperty(
                data_lake_principal_identifier=role.role_arn
            ),
            resource=lakeformation.CfnPermissions.ResourceProperty(
                database_resource=lakeformation.CfnPermissions.DatabaseResourceProperty(
                    catalog_id=access.account, name=database
                )
            ),
            permissions=["DESCRIBE", "SELECT"],
        )


__all__ = [
    "AGENT_MODEL_ACTIONS",
    "GOVERNED_ZONES",
    "LakeAccess",
    "apply_tags",
    "grant_answer_store",
    "grant_bedrock_invoke",
    "grant_config_read",
    "grant_governed_read",
    "grant_lake_formation",
    "grant_lake_writer",
    "is_destroyable",
    "log_retention",
]
