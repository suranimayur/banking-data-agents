"""The identity stack: Cognito for people, IAM roles for machines.

**Two audiences, two mechanisms.** Analysts authenticate with Cognito and receive
a role from a group; agents authenticate with an IAM role assumed by the AgentCore
runtime. Conflating them is how an agent ends up holding a human's credentials.

**Groups, not per-user permissions.** ``analyst``, ``investigator``,
``underwriter``, ``steward`` and ``auditor`` are the five roles the organisation
actually has, and the ``auditor`` group is read-only by construction: it is in no
role that can write anything, which is what makes "audit cannot mutate" a fact
rather than a policy statement.

**Roles are created here and granted data access elsewhere.** :mod:`identity`
never mentions a bucket. The data grants live in :func:`common.grant_governed_read`
and are applied by the runtime stack, so there is exactly one place to review what
an agent can read.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import apply_tags

#: Cognito groups. These are the organisation's roles, not the application's.
ACCESS_GROUPS: dict[str, str] = {
    "analyst": "Ask governed questions of the data products",
    "investigator": "Run fraud investigations and record dispositions",
    "underwriter": "Assess credit risk; outputs require human sign-off",
    "steward": "Own contracts, metrics and data quality rules",
    "auditor": "Read-only access to answers, evidence and lineage",
}


class IdentityStack(Stack):
    """Cognito user pool, console client, and the machine roles."""

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} identities and roles ({config.name})",
            **kwargs,
        )
        self.config = config

        # -- people ----------------------------------------------------------
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=qualify("analysts", config.name),
            self_sign_up_enabled=False,  # a bank does not let anyone self-register
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=14,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(1),
            ),
            # Threat protection at the user-pool level: compromised-credential detection
            # and adaptive authentication. Replaces the deprecated AdvancedSecurityMode.
            standard_threat_protection_mode=cognito.StandardThreatProtectionMode.FULL_FUNCTION,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=config.removal_policy,
        )

        self.groups: dict[str, cognito.CfnUserPoolGroup] = {
            name: cognito.CfnUserPoolGroup(
                self,
                f"Group{name.title()}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=name,
                description=description,
                precedence=index + 1,
            )
            for index, (name, description) in enumerate(ACCESS_GROUPS.items())
        }

        # -- machines --------------------------------------------------------
        # Machine roles deliberately do *not* live here. A role is granted access by
        # the stack that runs the workload — the pipeline role by the pipeline stack,
        # each agent's role by the runtime stack — so that every permission is
        # written in the same file as the thing that needs it, and so that no stack
        # has to reference another's role.

        # A public client: the browser is not a secret store. The SRP flow is what
        # the console uses, and it needs no callback URL — which matters, because the
        # console's own URL is created in a different stack and cannot be referenced
        # while this client is being defined. A hosted-UI OAuth flow is enabled only
        # when the stage declares where it is allowed to redirect to.
        oauth = None
        if config.console_callback_urls:
            oauth = cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=list(config.console_callback_urls),
            )

        self.console_client = cognito.UserPoolClient(
            self,
            "ConsoleClient",
            user_pool=self.user_pool,
            user_pool_client_name=qualify("console", config.name),
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=oauth,
        )

        apply_tags(self, config)

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "ConsoleClientId", value=self.console_client.user_pool_client_id)


__all__ = ["ACCESS_GROUPS", "IdentityStack"]
