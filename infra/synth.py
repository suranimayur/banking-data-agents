"""Composing the stacks for one environment, and synthesising them in-process.

Synthesis deliberately does **not** require the CDK CLI. The CLI is an npm package
that has to be installed and kept current, and the thing it actually does here —
``app.synth()`` — is a library call. Doing it in-process means ``bda infra synth``
works in CI, on a laptop and in a container with nothing installed but Python, and
it means the test suite can assert that every stack still synthesises.

Stack naming carries the environment (``BdaDevNetwork``, ``BdaProdNetwork``) so
that one account hosting several environments never has two stacks with the same
name, and so that a screwdriver-deep CloudFormation console screenshot is readable.

The image tag is a parameter, never a constant. A deploy pins an immutable git SHA;
``latest`` exists only so that a developer's first ``synth`` does not fail.
"""

from __future__ import annotations

import os
from dataclasses import replace

from aws_cdk import App, Environment

from infra.config import DEPLOYABLE, StageConfig, live_agent_versions, qualify, stage
from infra.stacks import (
    AgentRuntimeStack,
    ApiStack,
    CatalogStack,
    ConsoleStack,
    GatewayStack,
    GuardrailsStack,
    IdentityStack,
    NetworkStack,
    ObservabilityStack,
    PipelinesStack,
    StorageLakeStack,
)

#: Shown when someone asks for an environment that does not exist.
DEFAULT_STAGE = "dev"

#: Marker the pipeline uses to find the artifact it just built.
IMAGE_TAG_CONTEXT = "imageTag"

#: Synthetic "stage" name for the per-account bootstrap app. It is not an
#: environment — it is deployed once per account, by a human, before any of the
#: environments exist — but it is reachable through the same CLI so that there is
#: one way to synthesise infrastructure.
BOOTSTRAP_STAGE = "bootstrap"


def resolve_stage(app: App, requested: str | None = None) -> str:
    """Resolve which environment this app is being synthesised for.

    Precedence: explicit argument, CDK context (``-c stage=prod``), the
    ``BDA_STAGE`` environment variable, then ``dev``.
    """
    candidate = requested or app.node.try_get_context("stage") or os.environ.get("BDA_STAGE") or DEFAULT_STAGE
    name = str(candidate).strip().lower()
    if name not in DEPLOYABLE:
        raise KeyError(f"unknown stage '{candidate}'. Available: {', '.join(DEPLOYABLE)}")
    return name


def resolve_image_tag(app: App, requested: str | None = None) -> str:
    """Resolve the container image tag to deploy.

    CDK context wins over the environment variable, which wins over ``latest``.
    """
    candidate = requested or app.node.try_get_context(IMAGE_TAG_CONTEXT) or os.environ.get("BDA_IMAGE_TAG") or "latest"
    return str(candidate)


def build_app(
    *,
    stage_name: str | None = None,
    image_tag: str | None = None,
    outdir: str | None = None,
    app: App | None = None,
) -> App:
    """Build the CDK application for one environment.

    The stacks are wired by passing constructs between them rather than by looking
    resources up by name. CloudFormation then knows the dependency order, and a
    change to a bucket name cannot leave a stack quietly pointing at the old one.
    """
    app = app or App(outdir=outdir)

    stage_id = resolve_stage(app, stage_name)
    # A promotion is a configuration change, not a code change: the live endpoint is
    # pinned to the version the canary already served. Applying it here keeps every
    # stack reading one config object, and keeps the pin visible in the diff of a
    # `cdk diff` rather than hidden in an API call.
    config: StageConfig = replace(stage(stage_id), live_agent_versions=live_agent_versions())
    tag = resolve_image_tag(app, image_tag)

    # The account comes from the stage when known, and otherwise from the
    # environment the CLI is deploying into (``cdk deploy`` supplies both). The
    # region is always explicit: environment-agnostic stacks are rejected the
    # moment one stack references another, because a reference across regions is
    # only meaningful when the region is pinned. Pinning it here is what lets the
    # runtime stack reference the lake's KMS key instead of having to hard-code
    # the ARN it does not know yet.
    context_env = app.node.try_get_context("env") or {}
    env = Environment(
        account=config.account or context_env.get("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=config.region or context_env.get("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )

    prefix = f"Bda{stage_id.title()}"

    network = NetworkStack(app, f"{prefix}Network", config=config, env=env)
    identity = IdentityStack(app, f"{prefix}Identity", config=config, env=env)
    storage = StorageLakeStack(app, f"{prefix}StorageLake", config=config, env=env)
    catalog = CatalogStack(app, f"{prefix}Catalog", config=config, storage=storage, env=env)
    guardrails = GuardrailsStack(app, f"{prefix}Guardrails", config=config, storage=storage, env=env)

    pipelines = PipelinesStack(
        app,
        f"{prefix}Pipelines",
        config=config,
        network=network,
        storage=storage,
        catalog=catalog,
        image_tag=tag,
        env=env,
    )

    runtime = AgentRuntimeStack(
        app,
        f"{prefix}AgentRuntime",
        config=config,
        storage=storage,
        catalog=catalog,
        guardrails=guardrails,
        image_tag=tag,
        env=env,
    )

    api = ApiStack(
        app,
        f"{prefix}Api",
        config=config,
        storage=storage,
        identity=identity,
        image_tag=tag,
        env=env,
    )

    # The console is told where the API is, and the gateway publishes that same
    # surface as MCP tools. Both depend on the API being defined first.
    ConsoleStack(
        app,
        f"{prefix}Console",
        config=config,
        storage=storage,
        identity=identity,
        api_endpoint=api.service_url,
        image_tag=tag,
        env=env,
    )
    GatewayStack(
        app,
        f"{prefix}Gateway",
        config=config,
        storage=storage,
        api_endpoint=api.service_url,
        env=env,
    )
    ObservabilityStack(
        app,
        f"{prefix}Observability",
        config=config,
        storage=storage,
        runtimes=runtime.runtimes,
        workgroup_name=qualify("agents", config.name),
        pipeline_state_machine_arn=pipelines.state_machine.state_machine_arn,
        env=env,
    )

    return app


def build_assembly(
    *,
    stage_name: str | None = None,
    image_tag: str | None = None,
    outdir: str | None = None,
):
    """Synthesise whichever app the requested stage belongs to.

    One entry point for the CLI, two applications behind it: the per-environment
    stack set, and the once-per-account bootstrap.
    """
    if (stage_name or "").strip().lower() == BOOTSTRAP_STAGE:
        from infra.bootstrap.stack import build_app as build_bootstrap_app

        return build_bootstrap_app(outdir=outdir).synth()
    return build_app(stage_name=stage_name, image_tag=image_tag, outdir=outdir).synth()


def synth(
    *,
    stage_name: str | None = None,
    image_tag: str | None = None,
    outdir: str | None = None,
) -> list[str]:
    """Synthesise the app and return the stack names that were produced."""
    assembly = build_assembly(stage_name=stage_name, image_tag=image_tag, outdir=outdir)
    return [stack.stack_name for stack in assembly.stacks]


__all__ = [
    "BOOTSTRAP_STAGE",
    "DEFAULT_STAGE",
    "IMAGE_TAG_CONTEXT",
    "build_app",
    "build_assembly",
    "resolve_image_tag",
    "resolve_stage",
    "synth",
]
