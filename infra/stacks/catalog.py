"""The catalog stack: Glue databases, the governed Athena workgroup and LF-Tags.

Three things are established here, and each one exists to make a promise
mechanically true rather than aspirational.

**One Glue database per zone.** Permissions are granted per database, so "the
agents may read gold" has to be expressible as a policy. A single ``banking``
database would make the zone boundary a naming convention, and a naming
convention is not a control.

**The workgroup enforces its own scan ceiling.**
``enforce_work_group_configuration`` is what makes ``bytes_scanned_cutoff_per_query``
binding rather than advisory: a client cannot override it and run an accidental
full-warehouse scan on the bank's bill. This is the real cost control for the
agent layer, and it is why the workgroup is created here rather than left to
whoever runs the first query.

**Lake Formation tags, not per-table grants.** Zones and sensitivity are attached
as LF-Tags so that access is expressed as ``zone=gold AND sensitivity=internal``
and stays correct when a table is added tomorrow. Table-by-table grants decay;
tag-based grants do not.
"""

from __future__ import annotations

from aws_cdk import CfnTag, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_glue as glue
from aws_cdk import aws_lakeformation as lakeformation
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import GOVERNED_ZONES, apply_tags
from infra.stacks.storage_lake import StorageLakeStack

#: Glue catalog databases. One per zone, because permissions are granted per
#: database and "the agents may read gold" must be expressible in policy.
GLUE_DATABASES: tuple[str, ...] = ("bronze", "silver", "gold", "ops")

#: Human-readable titles, so construct ids read like the catalog they model.
DATABASE_TITLES: dict[str, str] = {
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold",
    "ops": "Ops",
}

#: LF-Tag keys. ``domain`` says what business area the data belongs to,
#: ``zone`` where it sits in the medallion, ``sensitivity`` how it may be handled.
TAG_KEYS: dict[str, list[str]] = {
    "domain": ["banking"],
    "zone": list(GLUE_DATABASES),
    "sensitivity": ["public", "internal", "confidential", "restricted"],
}


class CatalogStack(Stack):
    """Databases, the read-only workgroup and the Lake Formation taxonomy."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} catalog and query workgroup ({config.name})",
            **kwargs,
        )
        self.config = config
        self.storage = storage

        # -- databases -------------------------------------------------------
        self.databases: dict[str, glue.CfnDatabase] = {
            zone: glue.CfnDatabase(
                self,
                f"Glue{DATABASE_TITLES[zone]}",
                catalog_id=self.account,
                database_input=glue.CfnDatabase.DatabaseInputProperty(
                    name=self.database_name(zone),
                    description=f"{zone} zone of the {config.project} medallion lake",
                    # Parameters are how the pipeline finds the physical location
                    # of a zone without hard-coding a bucket name in a job script.
                    parameters={"classification": "parquet", "zone": zone},
                ),
            )
            for zone in GLUE_DATABASES
        }

        # -- query engine ----------------------------------------------------
        self.workgroup = athena.CfnWorkGroup(
            self,
            "Workgroup",
            name=qualify("agents", config.name),
            description=f"Agent query workgroup ({config.name}) with a hard scan ceiling",
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                # The two settings that make this a governed workgroup rather than
                # a convenient one: no client override, and a hard byte ceiling.
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=config.athena_bytes_cap_mb * 1024 * 1024,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{storage.artifacts_bucket.bucket_name}/athena-results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS",
                        kms_key=storage.key.key_arn,
                    ),
                ),
            ),
            tags=[CfnTag(key="Environment", value=config.name)],
        )

        # -- lake formation taxonomy -----------------------------------------
        # Not ``self.tags``: ``Stack.tags`` is the CDK tag manager.
        self.lf_tags: dict[str, lakeformation.CfnTag] = {
            key: lakeformation.CfnTag(self, f"Tag{key.title()}", tag_key=key, tag_values=values)
            for key, values in TAG_KEYS.items()
        }
        self.tag_associations: dict[str, lakeformation.CfnTagAssociation] = {
            zone: lakeformation.CfnTagAssociation(
                self,
                f"Associate{DATABASE_TITLES[zone]}",
                lf_tags=[
                    lakeformation.CfnTagAssociation.LFTagPairProperty(
                        catalog_id=self.account, tag_key="zone", tag_values=[zone]
                    ),
                    lakeformation.CfnTagAssociation.LFTagPairProperty(
                        catalog_id=self.account,
                        tag_key="sensitivity",
                        tag_values=["confidential" if zone in GOVERNED_ZONES else "restricted"],
                    ),
                ],
                resource=lakeformation.CfnTagAssociation.ResourceProperty(
                    database=lakeformation.CfnTagAssociation.DatabaseResourceProperty(
                        catalog_id=self.account, name=self.database_name(zone)
                    )
                ),
            )
            for zone in GLUE_DATABASES
        }

        apply_tags(self, config)

    # -- helpers -------------------------------------------------------------
    def database_name(self, zone: str) -> str:
        """Physical Glue database name for a zone, e.g. ``bda-prod-gold_db``."""
        return qualify(f"{zone}_db", self.config.name)

    def database_names(self) -> dict[str, str]:
        """Every zone's database name, as a plain mapping."""
        return {zone: self.database_name(zone) for zone in GLUE_DATABASES}

    @property
    def workgroup_arn(self) -> str:
        """The workgroup's ARN.

        Built rather than read off the construct: ``CfnWorkGroup`` exposes no ARN
        attribute, and the ARN is the only way to scope an ``athena:\
        StartQueryExecution`` grant to this workgroup instead of to all of them.
        """
        return f"arn:aws:athena:{self.region}:{self.account}:workgroup/{qualify('agents', self.config.name)}"


__all__ = ["DATABASE_TITLES", "GLUE_DATABASES", "TAG_KEYS", "CatalogStack"]
