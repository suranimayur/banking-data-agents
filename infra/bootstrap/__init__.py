"""The per-account bootstrap: everything that must exist before the pipeline can deploy.

Kept in its own package, with its own CDK app, because it is the one part of the
infrastructure that is *not* deployed by the pipeline. Bootstrapping yourself is a
privilege-escalation path, so this stack is deliberately outside the loop: a human
with administrator access deploys it once, reviews the trust condition it writes,
and then never touches it again.
"""

from __future__ import annotations

from infra.bootstrap.stack import (
    DEPLOY_ACTIONS,
    FORBIDDEN_ACTIONS,
    GITHUB_OIDC_ISSUER,
    BootstrapStack,
    build_app,
)

__all__ = [
    "DEPLOY_ACTIONS",
    "FORBIDDEN_ACTIONS",
    "GITHUB_OIDC_ISSUER",
    "BootstrapStack",
    "build_app",
]
