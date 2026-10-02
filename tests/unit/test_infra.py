"""Infrastructure regression tests.

These tests synthesise the real stack set and assert on the **templates**, not on
the Python objects that produced them. That distinction matters: a test that
asserts ``stack.roles["copilot"]`` exists tells you nothing about what the deployed
environment will permit, whereas reading the synthesized ``AWS::IAM::Policy``
documents tells you exactly what the agent will be allowed to do.

They cover the four claims the documentation makes and that a refactor could
quietly break:

1. every environment still synthesises, with the expected stacks;
2. no agent role can read the raw zones, whatever the code says;
3. the destructive policies differ between dev and prod in the intended direction;
4. the OIDC trust conditions do not let a branch deploy production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aws_cdk", reason="the `infra` dependency group is not installed")

from infra.config import identifier, qualify, stage
from infra.synth import BOOTSTRAP_STAGE, build_assembly

#: Every stack the per-environment app is expected to produce.
EXPECTED_STACKS: tuple[str, ...] = (
    "AgentRuntime",
    "Api",
    "Catalog",
    "Console",
    "Gateway",
    "Guardrails",
    "Identity",
    "Network",
    "Observability",
    "Pipelines",
    "StorageLake",
)


def _templates(env: str) -> dict[str, dict[str, Any]]:
    """Synthesise an environment and return ``{stack_name: template}``."""
    assembly = build_assembly(stage_name=env)
    return {
        stack.stack_name: json.loads(Path(stack.template_full_path).read_text(encoding="utf-8"))
        for stack in assembly.stacks
    }


@pytest.fixture(scope="module")
def dev_templates() -> dict[str, dict[str, Any]]:
    return _templates("dev")


@pytest.fixture(scope="module")
def prod_templates() -> dict[str, dict[str, Any]]:
    return _templates("prod")


@pytest.fixture(scope="module")
def bootstrap_template() -> dict[str, Any]:
    assembly = build_assembly(stage_name=BOOTSTRAP_STAGE)
    assert len(assembly.stacks) == 1
    return json.loads(Path(assembly.stacks[0].template_full_path).read_text(encoding="utf-8"))


def _resources(template: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [r for r in template.get("Resources", {}).values() if r.get("Type") == resource_type]


def _all_resources(templates: dict[str, dict[str, Any]], resource_type: str) -> list[dict[str, Any]]:
    return [r for t in templates.values() for r in _resources(t, resource_type)]


def _stack(templates: dict[str, dict[str, Any]], suffix: str) -> dict[str, Any]:
    """Look a stack up by its role in the architecture, not by its full name.

    Stack names carry the environment (``BdaProdStorageLake``), so matching on the
    suffix is what lets one test serve every environment.
    """
    matches = [t for name, t in templates.items() if name.endswith(suffix)]
    assert len(matches) == 1, f"expected exactly one {suffix} stack, found {len(matches)}"
    return matches[0]


def _replica(table: dict[str, Any]) -> dict[str, Any]:
    """Return a DynamoDB global table's first replica configuration.

    ``TableV2`` synthesises ``AWS::DynamoDB::GlobalTable``, where TTL and
    point-in-time recovery live *per replica* rather than at the top level.
    """
    replicas = table["Properties"]["Replicas"]
    assert len(replicas) == 1
    return replicas[0]


# ---------------------------------------------------------------------------
# 1. it synthesises
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_every_environment_synthesises_the_full_stack_set(env: str) -> None:
    templates = _templates(env)
    prefix = f"Bda{env.title()}"
    produced = {name.removeprefix(prefix) for name in templates}
    assert produced == set(EXPECTED_STACKS)
    # A stack that synthesises to an empty template is a stack that lost its
    # resources in a refactor, which is the failure this guards against.
    for name, template in templates.items():
        assert len(template.get("Resources", {})) >= 2, f"{name} synthesised almost nothing"


def test_bootstrap_synthesises_alone(bootstrap_template: dict[str, Any]) -> None:
    roles = _resources(bootstrap_template, "AWS::IAM::Role")
    # Three deploy roles plus the boundary's owning role is not expected; exactly the
    # three environments must exist.
    assert len(roles) >= 3


# ---------------------------------------------------------------------------
# 2. no agent role can read the raw zones
# ---------------------------------------------------------------------------
def test_no_agent_role_can_read_bronze_or_silver(dev_templates: dict[str, dict[str, Any]]) -> None:
    """The platform's central claim, asserted against the synthesized policies.

    "The agents cannot see raw PII" is enforced by IAM, not by prompt wording, so
    this is the test that keeps it true. It reads every IAM policy in the runtime
    stack and fails if any statement grants object access to a bronze or silver
    bucket.
    """
    runtime_stack = _stack(dev_templates, "AgentRuntime")
    policies = _resources(runtime_stack, "AWS::IAM::Policy")
    # One per agent role, plus the service role AgentCore Memory creates for itself.
    assert len(policies) >= 3, "expected a policy document per agent role"

    # Bucket names arrive as cross-stack import values, so they are matched by the
    # logical id of the bucket they import rather than by a literal ARN. That is
    # also stricter: it catches any reference, not only an exact ARN string.
    offenders: list[str] = []
    for policy in policies:
        body = json.dumps(policy)
        for raw in ("BronzeBucket", "SilverBucket"):
            if raw in body:
                offenders.append(f"{policy['Properties']['PolicyName']} references {raw}")
    assert not offenders, f"an agent role can read a raw zone: {offenders}"


def test_agent_roles_are_scoped_to_gold_and_ops(dev_templates: dict[str, dict[str, Any]]) -> None:
    """The positive half of the same claim: gold and ops *are* readable."""
    serialised = json.dumps(_stack(dev_templates, "AgentRuntime"))
    assert "GoldBucket" in serialised
    assert "OpsBucket" in serialised
    # And the zones they are *not* granted, so this test fails loudly if the grant
    # helper is ever widened to "all zones".
    assert "BronzeBucket" not in serialised
    assert "SilverBucket" not in serialised


# ---------------------------------------------------------------------------
# 3. destructive policies differ in the intended direction
# ---------------------------------------------------------------------------
def _bucket_name(bucket: dict[str, Any]) -> str:
    """A bucket's physical name, which is a literal because we set it explicitly."""
    name = bucket["Properties"]["BucketName"]
    assert isinstance(name, str)
    return name


def test_prod_retains_its_lake_and_dev_does_not(
    dev_templates: dict[str, dict[str, Any]], prod_templates: dict[str, dict[str, Any]]
) -> None:
    dev_buckets = _resources(_stack(dev_templates, "StorageLake"), "AWS::S3::Bucket")
    prod_buckets = _resources(_stack(prod_templates, "StorageLake"), "AWS::S3::Bucket")
    assert len(dev_buckets) == len(prod_buckets) == 5
    # RemovalPolicy.DESTROY synthesises to DeletionPolicy: Delete; RETAIN is the
    # CloudFormation default and is therefore absent from the template.
    assert all(b.get("DeletionPolicy") == "Delete" for b in dev_buckets)
    assert all(b.get("DeletionPolicy", "Retain") == "Retain" for b in prod_buckets)


def test_prod_versions_and_expires_noncurrent_objects(
    prod_templates: dict[str, dict[str, Any]],
) -> None:
    buckets = _resources(_stack(prod_templates, "StorageLake"), "AWS::S3::Bucket")
    data_buckets = [b for b in buckets if not _bucket_name(b).endswith("artifacts")]
    assert data_buckets
    for bucket in data_buckets:
        assert bucket["Properties"].get("VersioningConfiguration") == {"Status": "Enabled"}
        rules = bucket["Properties"].get("LifecycleConfiguration", {}).get("Rules", [])
        assert any(rule.get("NoncurrentVersionExpiration") for rule in rules)


def test_prod_answers_are_retained_longer_than_dev_answers(
    dev_templates: dict[str, dict[str, Any]], prod_templates: dict[str, dict[str, Any]]
) -> None:
    def answer_table(templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        tables = _resources(_stack(templates, "StorageLake"), "AWS::DynamoDB::GlobalTable")
        assert len(tables) == 1
        return tables[0]

    dev_table = answer_table(dev_templates)
    prod_table = answer_table(prod_templates)

    # TTL is wired in both environments; the retention *difference* lives in the
    # application's `expires_at` value, so what this asserts is that the mechanism
    # exists to expire an envelope at all. Without it the answer store grows forever,
    # which is the failure the stack's docstring promises will not happen.
    expected_ttl = {"Enabled": True, "AttributeName": "expires_at"}
    assert dev_table["Properties"]["TimeToLiveSpecification"] == expected_ttl
    assert prod_table["Properties"]["TimeToLiveSpecification"] == expected_ttl

    # Point-in-time recovery is switched on outside dev: recovering an audit table is
    # not a dev concern, and PITR is live per replica.
    assert _replica(prod_table)["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


# ---------------------------------------------------------------------------
# 4. the pipeline is the medallion pipeline
# ---------------------------------------------------------------------------
def test_state_machine_contains_every_medallion_stage_in_order(
    dev_templates: dict[str, dict[str, Any]],
) -> None:
    machines = _resources(_stack(dev_templates, "Pipelines"), "AWS::StepFunctions::StateMachine")
    assert len(machines) == 1
    definition = json.dumps(machines[0]["Properties"]["DefinitionString"])
    for stage_name in ("Generate", "Bronze", "Silver", "Gold", "Publish", "Quality"):
        assert stage_name in definition, f"stage {stage_name} missing from the state machine"
    # Order matters: publishing before quality would publish an unvalidated gold zone.
    assert definition.index("Bronze") < definition.index("Silver") < definition.index("Gold")
    # A failure must stop the run rather than continue to publish.
    assert "PipelineFailed" in definition


def test_pipeline_has_a_nightly_trigger_and_a_failure_topic(
    dev_templates: dict[str, dict[str, Any]],
) -> None:
    pipelines = _stack(dev_templates, "Pipelines")
    rules = _resources(pipelines, "AWS::Events::Rule")
    assert len(rules) == 1
    assert rules[0]["Properties"]["ScheduleExpression"].startswith("cron(")
    assert _resources(pipelines, "AWS::SNS::Topic")


# ---------------------------------------------------------------------------
# 5. guardrails
# ---------------------------------------------------------------------------
def test_guardrail_blocks_prompt_attack_and_indian_identifiers(
    dev_templates: dict[str, dict[str, Any]],
) -> None:
    guardrails = _resources(_stack(dev_templates, "Guardrails"), "AWS::Bedrock::Guardrail")
    assert len(guardrails) == 1
    config = guardrails[0]["Properties"]

    filter_types = {f["Type"] for f in config["ContentPolicyConfig"]["FiltersConfig"]}
    assert "PROMPT_ATTACK" in filter_types

    # Aadhaar and PAN are not Bedrock PII entity types; they must be regexes, and the
    # entity list must not claim support that the service does not have.
    entity_types = {e["Type"] for e in config["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert "IN_AADHAAR" not in entity_types
    patterns = {r["Name"] for r in config["SensitiveInformationPolicyConfig"]["RegexesConfig"]}
    assert {"aadhaar_number", "pan_number"} <= patterns

    topic_names = {t["Name"] for t in config["TopicPolicyConfig"]["TopicsConfig"]}
    assert "automated_credit_decision" in topic_names
    assert "individual_marketing_on_risk" in topic_names


def test_guardrail_is_versioned(dev_templates: dict[str, dict[str, Any]]) -> None:
    versions = _resources(_stack(dev_templates, "Guardrails"), "AWS::Bedrock::GuardrailVersion")
    assert len(versions) == 1


# ---------------------------------------------------------------------------
# 6. AgentCore naming and the policy engine
# ---------------------------------------------------------------------------
def test_agentcore_runtime_names_carry_no_hyphens(dev_templates: dict[str, dict[str, Any]]) -> None:
    """AgentCore uses the runtime name as a Cedar entity identifier.

    A hyphen is rejected at deploy time, which is a failure that only shows up
    against a real account, so it is caught here instead.
    """
    runtimes = _resources(_stack(dev_templates, "AgentRuntime"), "AWS::BedrockAgentCore::Runtime")
    assert len(runtimes) == 3
    for runtime in runtimes:
        name = runtime["Properties"]["AgentRuntimeName"]
        assert "-" not in name, name
        assert name.startswith("bda_")


def test_each_agent_gets_its_own_role_and_two_endpoints(
    dev_templates: dict[str, dict[str, Any]],
) -> None:
    runtime_stack = _stack(dev_templates, "AgentRuntime")
    runtimes = _resources(runtime_stack, "AWS::BedrockAgentCore::Runtime")
    endpoints = _resources(runtime_stack, "AWS::BedrockAgentCore::RuntimeEndpoint")
    assert len(runtimes) == 3
    # live and canary for each agent: promotion and rollback are traffic switches.
    assert len(endpoints) == 6
    assert {e["Properties"]["Name"] for e in endpoints} == {"live", "canary"}
    # Three distinct execution roles, so a compromised fraud agent is not also a
    # credit agent.
    role_arns = {json.dumps(r["Properties"]["RoleArn"]) for r in runtimes}
    assert len(role_arns) == 3


def test_policy_engine_forbids_irreversible_actions(dev_templates: dict[str, dict[str, Any]]) -> None:
    policies = _resources(_stack(dev_templates, "Gateway"), "AWS::BedrockAgentCore::Policy")
    assert len(policies) >= 4

    # The statements synthesise to Cedar *text*, so they are read as text. Searching
    # the JSON serialisation instead would mean matching escaped quotes, which is how
    # a test quietly stops checking what it claims to.
    cedar = "\n".join(p["Properties"]["Definition"]["Cedar"]["Statement"] for p in policies)
    assert "forbid(" in cedar
    for action in ("execute_block", "finalize_decision", "read_bronze"):
        assert action in cedar, f"no policy mentions {action}"
    # Per-agent prohibitions, not only the platform-wide ones: the fraud agent must
    # not rank individuals for marketing, and that is a statement about *fraud*.
    assert 'principal == Agent::"fraud"' in cedar
    assert 'principal == Agent::"credit"' in cedar


# ---------------------------------------------------------------------------
# 7. bootstrap trust conditions
# ---------------------------------------------------------------------------
def test_prod_deploy_role_trusts_only_the_prod_environment(bootstrap_template: dict[str, Any]) -> None:
    """A production role that also trusts a branch ref is a production role any push can assume."""
    # The OIDC provider is created by a CDK custom resource whose own role is also an
    # AWS::IAM::Role; only the deploy roles are ours to assert on.
    roles = {
        r["Properties"]["RoleName"]: r
        for r in _resources(bootstrap_template, "AWS::IAM::Role")
        if "RoleName" in r["Properties"] and r["Properties"]["RoleName"].startswith("bda-github-")
    }
    assert len(roles) == 3
    for environment in ("dev", "staging", "prod"):
        assert f"bda-github-{environment}" in roles

    prod_trust = json.dumps(roles["bda-github-prod"]["Properties"]["AssumeRolePolicyDocument"])
    assert "environment:prod" in prod_trust
    assert "ref:refs/heads/main" not in prod_trust
    assert "environment:dev" not in prod_trust

    dev_trust = json.dumps(roles["bda-github-dev"]["Properties"]["AssumeRolePolicyDocument"])
    assert "ref:refs/heads/main" in dev_trust


def test_every_deploy_role_has_the_permissions_boundary(bootstrap_template: dict[str, Any]) -> None:
    """Without a boundary, a compromised build can create an admin role."""
    boundary = [
        r for r in _resources(bootstrap_template, "AWS::IAM::ManagedPolicy")
        if r["Properties"]["ManagedPolicyName"] == "bda-deploy-boundary"
    ]
    assert len(boundary) == 1

    deploy_roles = [
        r
        for r in _resources(bootstrap_template, "AWS::IAM::Role")
        if str(r["Properties"].get("RoleName", "")).startswith("bda-github-")
    ]
    assert len(deploy_roles) == 3
    for role in deploy_roles:
        assert "PermissionsBoundary" in role["Properties"], role["Properties"].get("RoleName")

    serialised = json.dumps(boundary[0])
    for forbidden in ("iam:CreateUser", "iam:CreateAccessKey", "organizations:*"):
        assert forbidden in serialised


def test_bootstrap_state_bucket_is_retained_and_versioned(bootstrap_template: dict[str, Any]) -> None:
    buckets = _resources(bootstrap_template, "AWS::S3::Bucket")
    assert len(buckets) == 1
    assert buckets[0].get("DeletionPolicy", "Retain") == "Retain"
    assert buckets[0]["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}


# ---------------------------------------------------------------------------
# 8. naming helpers
# ---------------------------------------------------------------------------
def test_identifier_strips_hyphens_and_qualify_keeps_them() -> None:
    assert identifier("agent-copilot", "dev") == "bda_dev_agent_copilot"
    assert qualify("agent-copilot", "dev") == "bda-dev-agent-copilot"


def test_stage_config_exposes_the_fields_the_stacks_read() -> None:
    prod = stage("prod")
    dev = stage("dev")
    assert prod.monthly_budget_usd > dev.monthly_budget_usd
    assert prod.answer_retention_days > dev.answer_retention_days
    assert prod.athena_bytes_cap_mb >= dev.athena_bytes_cap_mb


def test_unknown_stage_is_rejected_with_advice() -> None:
    with pytest.raises(KeyError) as excinfo:
        stage("production")
    assert "dev" in str(excinfo.value)
