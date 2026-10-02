"""``bda infra`` — synthesise, deploy and destroy.

The command exists so that a human never types ``cdk`` directly. Not because the
CDK CLI is hostile, but because every hand-typed deploy is a deploy that skipped
the eval gate, used a mutable image tag, and was not reproducible afterwards.

Three properties are enforced here rather than trusted to a runbook:

**Synth needs nothing but Python.** It calls the library in-process, so it works in
CI and in a container with no npm.

**Deploy is gated, and ``prod`` is gated twice.** A non-interactive shell cannot
deploy any environment without ``--yes`` (which is what the pipeline passes after
its own approvals), and ``prod`` additionally requires ``BDA_ALLOW_PROD_DEPLOY=1``.
Belt and braces on purpose: a stray ``bda infra deploy --env prod --yes`` in a
terminal should not reach the bank's production account.

**The image tag is required for anything but dev.** ``latest`` is how a deploy
becomes unreproducible; a deployment that cannot name the commit it is running is a
deployment that cannot be rolled back to anything specific.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from banking_data_agents.config import get_settings

#: Set this in the CI environment (never in a laptop shell profile) to permit a
#: production deploy. The deploy-prod workflow sets it after human approval.
PROD_DEPLOY_ENV_VAR = "BDA_ALLOW_PROD_DEPLOY"

#: Where synthesised templates land. Inside the project, and gitignored.
CDK_OUT_DIR = "cdk.out"


def project_root() -> Path:
    """Repository root, resolved the same way the settings module resolves it."""
    return get_settings().data_dir.parent


def ensure_infra_on_path() -> Path:
    """Make the top-level ``infra`` package importable from the installed CLI.

    ``infra/`` is deliberately *not* packaged into the wheel — deployment code has
    no place in the runtime artifact — so when ``bda`` is invoked through its
    console-script entry point, the repository root is not on ``sys.path`` and
    ``import infra`` fails. This puts it there, once, instead of scattering
    ``sys.path`` edits through the command handlers.
    """
    root = project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def _require_cdk_library() -> None:
    """Fail with advice rather than a traceback when the infra group is absent."""
    try:
        import aws_cdk  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the caller's env
        raise SystemExit(
            "[bda] the CDK library is not installed in this environment.\n"
            "      install it with:  uv sync --group infra\n"
            "      or run:           uv run --group infra bda infra synth"
        ) from exc


def _require_cdk_cli() -> str:
    """Return the CDK CLI path, or explain how to get one."""
    executable = shutil.which("cdk")
    if executable is None:
        raise SystemExit(
            "[bda] the AWS CDK CLI is not on PATH. Synthesis does not need it, but deploy does.\n"
            "      install it with:  npm install -g aws-cdk\n"
            "      in CI, use:       npx --yes aws-cdk@2"
        )
    return executable


def _confirm(prompt: str) -> bool:
    """Ask for a yes. A non-interactive shell counts as a no."""
    if not sys.stdin.isatty():
        print("[bda] refusing to continue: no terminal to confirm on. Pass --yes to proceed.", file=sys.stderr)
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _stage_or_exit(env: str):
    """Resolve a stage name, or exit with the list of valid ones."""
    from infra.config import stage

    try:
        return stage(env)
    except KeyError as exc:
        raise SystemExit(f"[bda] {exc}") from exc


def _app_command(env: str, image_tag: str | None) -> str:
    """The ``--app`` value that makes the CDK CLI use our builder.

    ``sys.executable`` rather than ``python``: the CLI may be run under a different
    interpreter than the one holding the CDK library.
    """
    from infra.synth import BOOTSTRAP_STAGE

    if env.strip().lower() == BOOTSTRAP_STAGE:
        # The bootstrap has its own app: different inputs (account-wide, not
        # per-environment) and a different lifetime (once, not per release).
        return f"{sys.executable} -m infra.bootstrap.app"

    command = f"{sys.executable} -m infra.app -c stage={env}"
    if image_tag:
        command += f" -c imageTag={image_tag}"
    return command


def synth(env: str = "dev", *, image_tag: str | None = None) -> int:
    """Synthesise every stack for an environment, without touching AWS.

    Prints one line per stack with the size of its template, because "it
    synthesised" and "it synthesised something" are different claims: a stack whose
    template shrank by 80% is usually a stack that lost half its resources.
    """
    _require_cdk_library()
    ensure_infra_on_path()

    from infra.synth import BOOTSTRAP_STAGE, build_assembly

    is_bootstrap = env.strip().lower() == BOOTSTRAP_STAGE
    if not is_bootstrap:
        _stage_or_exit(env)

    outdir = project_root() / CDK_OUT_DIR / env
    assembly = build_assembly(stage_name=env, image_tag=image_tag, outdir=str(outdir))

    if is_bootstrap:
        _warn_about_placeholder_oidc()

    print(f"[bda] synthesised {len(assembly.stacks)} stacks for env={env} → {outdir}")
    for stack in sorted(assembly.stacks, key=lambda s: s.stack_name):
        template = stack.template_file
        path = Path(stack.template_full_path)
        size = path.stat().st_size if path.exists() else 0
        resources = _count_resources(path)
        print(f"      {stack.stack_name:<28} {resources:>4} resources  {size / 1024:>7.1f} KiB  ({template})")
    return 0


def _warn_about_placeholder_oidc() -> None:
    """Point out that the OIDC trust condition still names the shipped example repo.

    A bootstrap stack whose trust policy points at the wrong repository either
    fails to let the real repository in, or — worse — lets somebody else's in. It
    is worth one loud line at synth time.
    """
    from infra.config import bootstrap_config

    config = bootstrap_config()
    if config.is_placeholder:
        print(
            "[bda] NOTE: the OIDC trust policy still names 'your-org/banking-data-agents'.\n"
            "      set BDA_GITHUB_OWNER and BDA_GITHUB_REPO before deploying the bootstrap stack."
        )
    print(f"[bda] GitHub OIDC subject prefix: repo:{config.repository}")
    for environment in config.environments:
        print(f"      bda-github-{environment} trusts repo:{config.repository}:environment:{environment}")


def _count_resources(template_path: Path) -> int:
    """Count resources in a synthesised template, tolerating anything unexpected."""
    import json

    try:
        body = json.loads(template_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    resources = body.get("Resources")
    return len(resources) if isinstance(resources, dict) else 0


def deploy(
    env: str = "dev",
    *,
    require_approval: bool = True,
    image_tag: str | None = None,
    stacks: list[str] | None = None,
) -> int:
    """Deploy stacks for an environment through the CDK CLI.

    The CLI is required here because a deploy is a sequence of API calls with
    resumability, change sets and asset publishing that is not worth
    reimplementing. Everything before and after it — the eval gate, the smoke
    tests, the canary watch — belongs to the pipeline.

    ``stacks`` narrows the deploy to named stacks, which is how the data pipeline
    workflow ships a change to the medallion run without touching the agents.
    """
    from infra.config import BootstrapConfig
    from infra.synth import BOOTSTRAP_STAGE

    is_bootstrap = env.strip().lower() == BOOTSTRAP_STAGE
    cdk = _require_cdk_cli()

    if is_bootstrap:
        # Named separately from the stage branch so that "which stack set, in which
        # account" is unambiguous in the confirmation prompt and in the logs.
        label = BootstrapConfig().github_repo
        target = "the account (admin access required)"
        print(
            "[bda] bootstrapping an account creates the deploy roles and the permissions\n"
            "      boundary, so it needs administrator rights. Deploy it once, review the\n"
            "      trust policy it writes, then leave it alone."
        )
    else:
        stage_config = _stage_or_exit(env)
        label = stage_config.name
        target = stage_config.account or "(account from the CLI environment)"

    if env == "prod" and os.environ.get(PROD_DEPLOY_ENV_VAR) != "1":
        print(
            f"[bda] refusing to deploy to prod from a terminal.\n"
            f"      Production is deployed by .github/workflows/deploy-prod.yml, which requires\n"
            f"      a GitHub Environment approval. To override deliberately, set {PROD_DEPLOY_ENV_VAR}=1.",
            file=sys.stderr,
        )
        return 2

    if require_approval and not _confirm(f"Apply the {label} stack set to {target}?"):
        print("[bda] aborted.")
        return 1

    if env != "dev" and not image_tag and not stacks:
        print(
            "[bda] refusing to deploy a non-dev environment without an explicit image tag.\n"
            "      'latest' makes the deployment unreproducible. Pass --image-tag <git-sha>.",
            file=sys.stderr,
        )
        return 2

    # ``--all`` and explicit stack names are mutually exclusive in the CDK CLI.
    targets = list(stacks) if stacks else ["--all"]

    command = [
        cdk,
        "deploy",
        *targets,
        "--app",
        _app_command(env, image_tag),
        "--require-approval",
        "never",  # our own gate has already run; a second prompt would hang CI
        "--outputs-file",
        str(project_root() / CDK_OUT_DIR / f"{env}-outputs.json"),
    ]
    scope = ", ".join(stacks) if stacks else "all stacks"
    print(f"[bda] deploying {scope} (env={env}, image={image_tag or 'latest'}) …")
    return subprocess.call(command, cwd=project_root())


def destroy(env: str = "dev", *, force: bool = False) -> int:
    """Tear down an environment.

    ``prod`` refuses outright. Tearing down production is a change-managed
    activity with a paper trail, not a shell command, and a flag that overrides
    that is a flag that will eventually be typed at 3am.
    """
    from infra.synth import BOOTSTRAP_STAGE

    if env.strip().lower() == BOOTSTRAP_STAGE:
        print(
            "[bda] refusing to destroy the bootstrap stack. It holds the deploy roles and the\n"
            "      state bucket; removing it does not tear down an environment, it removes the\n"
            "      ability to manage any of them.",
            file=sys.stderr,
        )
        return 2

    config = _stage_or_exit(env)
    if env == "prod":
        print(
            "[bda] refusing to destroy prod. Retire the stacks through a change record\n"
            "      and a reviewed workflow, not from a command line.",
            file=sys.stderr,
        )
        return 2

    cdk = _require_cdk_cli()
    warning = f"Destroy {config.name}? " + (
        "This deletes the lake and the answer store (dev is configured to delete)."
        if config.removal_policy == 1
        else "Data buckets are retained, but catalog tables and the runtime are removed."
    )
    if not force and not _confirm(warning):
        print("[bda] aborted.")
        return 1

    command = [
        cdk,
        "destroy",
        "--all",
        "--app",
        _app_command(env, None),
        "--force",
    ]
    print(f"[bda] destroying env={env} …")
    return subprocess.call(command, cwd=project_root())


__all__ = [
    "CDK_OUT_DIR",
    "PROD_DEPLOY_ENV_VAR",
    "deploy",
    "destroy",
    "ensure_infra_on_path",
    "project_root",
    "synth",
]
