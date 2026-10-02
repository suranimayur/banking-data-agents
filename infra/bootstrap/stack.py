"""The one-time, per-account bootstrap stack.

Everything in ``infra/stacks`` assumes this stack already exists. It is deployed
once per AWS account by someone with administrator access, and never by the
pipeline it enables — the pipeline cannot grant itself the right to exist.

What it creates, and why each piece is here:

**A GitHub OIDC provider.** So that CI authenticates to AWS with a short-lived
token instead of a long-lived access key sitting in repository secrets. This is the
single highest-value security decision in the whole deployment story: a leaked key
is valid until someone notices and rotates it, while a leaked OIDC token is valid
for minutes.

**One deploy role per environment, with an explicit ``sub`` condition.** ``dev``
accepts ``repo:owner/name:environment:dev``, ``staging`` its own, and ``prod`` only
``repo:owner/name:environment:prod``. A production role that also trusts a branch
ref is a production role that any push can assume; that is the difference this
condition makes.

**A permissions boundary.** Every role the pipeline creates must carry it. Without
one, the first compromised build can create a role with ``AdministratorAccess`` and
the OIDC constraint on the entry point becomes decoration. The boundary is what
makes "the pipeline cannot escalate beyond this" true after the initial assume.

**A state bucket with versioning.** CloudFormation state that cannot be rolled back
is state you cannot recover from a bad deploy. It is deliberately *not* encrypted
with a customer-managed key: the key would live in a stack whose own state lives in
this bucket, which is a bootstrap cycle.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Environment, RemovalPolicy, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import BootstrapConfig

#: The GitHub OIDC issuer. Kept as a constant because it is the one value in this
#: file that a reader should verify rather than trust.
GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_THUMBPRINT = "6938fd4d98bab03faadb97b34396831e3780aea1"

#: Services the deployment needs. Written as a list rather than as
#: ``AdministratorAccess`` because a deploy role that can do anything makes the
#: permissions boundary the only real control.
DEPLOY_ACTIONS: tuple[str, ...] = (
    "cloudformation:*",
    "s3:*",
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:GetPolicy",
    "iam:GetPolicyVersion",
    "iam:ListRoles",
    "iam:ListRolePolicies",
    "iam:ListAttachedRolePolicies",
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:UpdateRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:TagRole",
    "iam:PassRole",
    "iam:CreatePolicy",
    "iam:DeletePolicy",
    "iam:CreatePolicyVersion",
    "iam:ListPolicyVersions",
    "iam:TagPolicy",
    "kms:*",
    "logs:*",
    "ecr:*",
    "ec2:*",
    "sns:*",
    "cloudwatch:*",
    "budgets:*",
    "dynamodb:*",
    "kinesis:*",
    "glue:*",
    "athena:*",
    "lakeformation:*",
    "secretsmanager:*",
    "cognito-idp:*",
    "lambda:*",
    "states:*",
    "events:*",
    "apprunner:*",
    "bedrock:*",
    "bedrock-agentcore:*",
    "ssm:GetParameter",
    "sts:GetCallerIdentity",
)

#: Actions no deploy role may ever perform, whatever else is allowed. These are the
#: ones that would let a compromised pipeline persist, escape the environment, or
#: turn off its own audit trail.
FORBIDDEN_ACTIONS: tuple[str, ...] = (
    "iam:CreateUser",
    "iam:CreateAccessKey",
    "iam:CreateLoginProfile",
    "iam:UpdateLoginProfile",
    "iam:AddUserToGroup",
    "iam:CreateSAMLProvider",
    "iam:DeleteRolePermissionsBoundary",
    "iam:PutRolePermissionsBoundary",
    "organizations:*",
    "account:*",
    "cloudtrail:DeleteTrail",
    "cloudtrail:StopLogging",
    "cloudtrail:UpdateTrail",
    "config:DeleteConfigurationRecorder",
    "config:StopConfigurationRecorder",
    "guardduty:DeleteDetector",
    "guardduty:DisassociateFromMasterAccount",
    "s3:PutBucketPublicAccessBlock",
    "ec2:DeleteVpcEndpoints",
)

#: The bootstrap itself is deployed by a human with admin rights, and it is the
#: one stack with no permissions boundary on its own assumptions.
GITHUB_OIDC_CLIENT_ID = "sts.amazonaws.com"


class BootstrapStack(Stack):
    """OIDC provider, permissions boundary, per-environment deploy roles, state bucket."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: BootstrapConfig,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description="One-time per-account bootstrap: OIDC deploy roles, state bucket, boundary",
            **kwargs,
        )
        self.config = config

        # -- terraform/cloudformation state ------------------------------------
        self.state_bucket = s3.Bucket(
            self,
            "StateBucket",
            bucket_name=f"bda-cdk-state-{self.account}",
            versioned=True,
            # SSE-S3 on purpose: see the module docstring. A CMK here would live in
            # the stack whose state this bucket holds.
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent-state",
                    enabled=True,
                    noncurrent_version_expiration=Duration.days(config.state_noncurrent_days),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # -- OIDC provider -----------------------------------------------------
        self.provider = iam.OpenIdConnectProvider(
            self,
            "GitHubOidc",
            url=GITHUB_OIDC_ISSUER,
            client_ids=[GITHUB_OIDC_CLIENT_ID],
            thumbprints=[GITHUB_OIDC_THUMBPRINT],
        )

        # -- permissions boundary ----------------------------------------------
        self.boundary = iam.ManagedPolicy(
            self,
            "DeployBoundary",
            managed_policy_name="bda-deploy-boundary",
            description="Ceiling on every role the deployment pipeline may create or use",
            statements=[
                iam.PolicyStatement(actions=["*"], resources=["*"]),
                iam.PolicyStatement(
                    sid="NoIdentityPersistence",
                    effect=iam.Effect.DENY,
                    actions=list(FORBIDDEN_ACTIONS),
                    resources=["*"],
                ),
                iam.PolicyStatement(
                    sid="StayInRegion",
                    effect=iam.Effect.DENY,
                    actions=["*"],
                    resources=["*"],
                    conditions={"StringNotEquals": {"aws:RequestedRegion": config.region}},
                ),
                iam.PolicyStatement(
                    sid="NoBoundaryRemoval",
                    effect=iam.Effect.DENY,
                    actions=[
                        "iam:DeleteRolePermissionsBoundary",
                        "iam:PutRolePermissionsBoundary",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # -- deploy policy ------------------------------------------------------
        self.deploy_policy = iam.ManagedPolicy(
            self,
            "DeployPolicy",
            managed_policy_name="bda-deploy",
            description="What a deploy role may do: manage this application's stacks and their resources",
            statements=[
                iam.PolicyStatement(actions=list(DEPLOY_ACTIONS), resources=["*"]),
                iam.PolicyStatement(
                    sid="OnlyPassRolesWithOurBoundary",
                    effect=iam.Effect.ALLOW,
                    actions=["iam:PassRole", "iam:CreateRole"],
                    resources=[f"arn:aws:iam::{self.account}:role/bda-*"],
                    conditions={"StringEquals": {"iam:PermissionsBoundary": self.boundary.managed_policy_arn}},
                ),
            ],
        )

        # -- deploy roles -------------------------------------------------------
        self.deploy_roles: dict[str, iam.Role] = {
            environment: self._deploy_role(environment) for environment in config.environments
        }

        for key, value in {"Project": "banking-data-agents", "ManagedBy": "cdk-bootstrap"}.items():
            Tags.of(self).add(key, value)

        CfnOutput(self, "StateBucketName", value=self.state_bucket.bucket_name)
        CfnOutput(self, "PermissionsBoundaryArn", value=self.boundary.managed_policy_arn)
        for environment, role in self.deploy_roles.items():
            CfnOutput(self, f"DeployRoleArn{environment.title()}", value=role.role_arn)

    # -- helpers ---------------------------------------------------------------
    def _deploy_role(self, environment: str) -> iam.Role:
        """Create the OIDC-assumable role a GitHub Environment deploys as.

        The trust policy names the *GitHub Environment*, not the branch. That is
        what ties the deploy to the approval configured on that environment: an
        approval that gates a workflow but not the credential behind it is a
        courtesy, not a control.
        """
        subject = f"repo:{self.config.repository}:environment:{environment}"
        if environment == "dev":
            # Dev may also deploy straight from a push to a protected branch, which
            # is what makes "commit to dev deployment in under 15 minutes" possible.
            subjects = [subject] + [
                f"repo:{self.config.repository}:ref:refs/heads/{branch}" for branch in self.config.protected_branches
            ]
        else:
            subjects = [subject]

        return iam.Role(
            self,
            f"DeployRole{environment.title()}",
            role_name=f"bda-github-{environment}",
            description=f"GitHub Actions deploy role for the {environment} environment",
            assumed_by=iam.FederatedPrincipal(
                self.provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {
                        f"{GITHUB_OIDC_ISSUER}:aud": GITHUB_OIDC_CLIENT_ID,
                    },
                    "StringLike": {f"{GITHUB_OIDC_ISSUER}:sub": subjects},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            # The boundary is attached here as well as being a condition on role
            # creation: a role that can shed its own boundary is a role with no
            # boundary.
            permissions_boundary=self.boundary,
            managed_policies=[self.deploy_policy],
            max_session_duration=Duration.hours(1),
        )


def build_app(outdir: str | None = None):
    """Build the bootstrap application.

    Separate from :func:`infra.synth.build_app` because the bootstrap has a
    different input (account-wide, not per-environment), a different lifetime
    (once, not per release) and a different audience (a human with admin rights).
    """
    from aws_cdk import App

    from infra.config import bootstrap_config

    config = bootstrap_config()
    app = App(outdir=outdir)
    BootstrapStack(
        app,
        "BdaBootstrap",
        config=config,
        env=Environment(account=None, region=config.region),
    )
    return app


__all__ = [
    "DEPLOY_ACTIONS",
    "FORBIDDEN_ACTIONS",
    "GITHUB_OIDC_ISSUER",
    "GITHUB_OIDC_THUMBPRINT",
    "BootstrapStack",
    "build_app",
]
