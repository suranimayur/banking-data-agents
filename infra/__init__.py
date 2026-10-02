"""AWS CDK infrastructure for the Banking Data Agents platform.

This package is a *deployment* artifact, not part of the shipped Python
application: it is not included in the wheel, it is not imported by any runtime
code path, and it is only installed in the ``infra`` dependency group.

The layering is deliberate and matches the app's own layering:

``config``
    The only place where ``dev`` / ``staging`` / ``prod`` differ. A stack never
    branches on the environment name; it reads a field from :class:`StageConfig`.
``stacks``
    One module per logical system, each owning the resources it is named for.
``app``
    Composes the stacks in dependency order for one environment.
``bootstrap``
    The one-time, per-account stack (OIDC deploy roles, state bucket, ECR
    repositories) that has to exist *before* any of the above can be deployed.

Synthesis needs no AWS credentials and no CDK CLI: ``python -m infra.app`` and
``bda infra synth`` both call ``app.synth()`` in-process.
"""
