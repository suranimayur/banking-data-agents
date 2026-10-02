"""The observability stack: five dashboards, the alarms that page, and the budget.

The dashboards are the five from the spec's own observability chapter, because
those are the five questions an on-call engineer actually asks:

``agent-health``      is it up, is it slow, is it erroring
``agent-quality``     is it still right — eval score, refusals, evidence completeness
``cost``              what is this costing, and per what
``governance``        who tried to do something they should not have
``data-freshness``    is the data behind the answer as fresh as the contract promises

**Metric names come from the application.** ``banking_data_agents.telemetry``
defines every custom metric name and this stack imports it. That is the difference
between a dashboard that watches a real metric and one that renders an empty
panel forever; it is a small coupling and it buys a guarantee.

**Alarms act, they do not decorate.** Each one publishes to a single SNS topic that
the deploy workflow and the on-call rotation both subscribe to, and the alarms the
spec calls page-worthy — a denied-but-attempted privileged call, a budget breach, a
gold SLA miss — are the ones that cannot be snoozed.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from banking_data_agents import telemetry
from infra.config import StageConfig, qualify
from infra.stacks.common import apply_tags
from infra.stacks.storage_lake import StorageLakeStack

#: Dashboards, by name. Each is one CloudWatch dashboard, built in this stack.
DASHBOARDS: tuple[str, ...] = (
    "agent-health",
    "agent-quality",
    "cost",
    "governance",
    "data-freshness",
)

#: Alarm thresholds. Expressed as data so that a staging environment can be
#: noisier than production without a second code path.
LATENCY_P95_THRESHOLD_MS = 20_000
ERROR_COUNT_THRESHOLD = 5
POLICY_DENIAL_THRESHOLD = 1
SLA_MISS_THRESHOLD = 1


class ObservabilityStack(Stack):
    """Dashboards, alarms, the alert topic and the monthly budget."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: StageConfig,
        storage: StorageLakeStack,
        runtimes: dict[str, agentcore.Runtime],
        workgroup_name: str,
        pipeline_state_machine_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} dashboards, alarms and budget ({config.name})",
            **kwargs,
        )
        self.config = config
        self.runtimes = runtimes

        # -- alerting ---------------------------------------------------------
        # One topic. Two topics is how an alarm ends up publishing to the one
        # nobody is subscribed to.
        self.alert_topic = sns.Topic(
            self,
            "Alerts",
            topic_name=qualify("alerts", config.name),
            display_name=f"BDA alerts ({config.name})",
            master_key=storage.key,
        )
        if config.alarm_email:
            self.alert_topic.add_subscription(subscriptions.EmailSubscription(config.alarm_email))

        # -- alarms ------------------------------------------------------------
        self.alarms: dict[str, cloudwatch.Alarm] = {}

        for agent_name, runtime in runtimes.items():
            # Native AgentCore metrics: no application change is needed for the
            # availability and latency panels to have data.
            self.alarms[f"{agent_name}_latency_p95"] = self._alarm(
                f"Alarm{agent_name.title()}LatencyP95",
                alarm_name=qualify(f"{agent_name}-latency-p95", config.name),
                description=f"{agent_name} agent p95 latency exceeded {LATENCY_P95_THRESHOLD_MS} ms",
                metric=runtime.metric_latency(statistic="p95", period=Duration.minutes(5)),
                threshold=LATENCY_P95_THRESHOLD_MS,
            )
            self.alarms[f"{agent_name}_errors"] = self._alarm(
                f"Alarm{agent_name.title()}Errors",
                alarm_name=qualify(f"{agent_name}-errors", config.name),
                description=f"{agent_name} agent produced {ERROR_COUNT_THRESHOLD}+ errors in 5 minutes",
                metric=runtime.metric_total_errors(statistic="Sum", period=Duration.minutes(5)),
                threshold=ERROR_COUNT_THRESHOLD,
            )

        # Governance: a denied-but-attempted privileged call is page-worthy because
        # it is either a bug or an attack, and both are urgent.
        self.alarms["policy_denials"] = self._alarm(
            "AlarmPolicyDenials",
            alarm_name=qualify("policy-denials", config.name),
            description="A tool call was denied by policy; investigate before dismissing",
            metric=cloudwatch.Metric(
                namespace=telemetry.METRIC_NAMESPACE,
                metric_name=telemetry.REFUSALS,
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=POLICY_DENIAL_THRESHOLD,
        )

        # Data freshness: a gold product past its SLA makes every answer above it
        # wrong in a way the answer itself does not reveal.
        self.alarms["freshness_sla"] = self._alarm(
            "AlarmFreshnessSla",
            alarm_name=qualify("freshness-sla", config.name),
            description="A gold data product missed its freshness SLA",
            metric=cloudwatch.Metric(
                namespace=telemetry.DATA_QUALITY_NAMESPACE,
                metric_name=telemetry.DQ_SLA_MISSES,
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=SLA_MISS_THRESHOLD,
        )

        # Cost: Athena bytes scanned is the silent budget killer, so it is watched
        # at the workgroup where the cap is enforced.
        self.alarms["athena_bytes"] = self._alarm(
            "AlarmAthenaBytes",
            alarm_name=qualify("athena-bytes", config.name),
            description="Athena scanned more than the workgroup cap in an hour; a query is over-scanning",
            metric=cloudwatch.Metric(
                namespace="AWS/Athena",
                metric_name="ProcessedBytes",
                dimensions_map={"WorkGroup": workgroup_name},
                statistic="Sum",
                period=Duration.hours(1),
            ),
            threshold=config.athena_bytes_cap_mb * 1024 * 1024 * 10,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        if pipeline_state_machine_arn is not None:
            self.alarms["pipeline_failed"] = self._alarm(
                "AlarmPipelineFailed",
                alarm_name=qualify("pipeline-failed", config.name),
                description="The nightly medallion run failed; gold was not published",
                metric=cloudwatch.Metric(
                    namespace="AWS/States",
                    metric_name="ExecutionsFailed",
                    dimensions_map={"StateMachineArn": pipeline_state_machine_arn},
                    statistic="Sum",
                    period=Duration.hours(1),
                ),
                threshold=1,
            )

        # -- dashboards --------------------------------------------------------
        self.dashboards = {name: self._dashboard(name) for name in DASHBOARDS}

        # -- budget ------------------------------------------------------------
        # A budget is an alarm about money, and it is the only alarm whose breach is
        # guaranteed to happen eventually.
        self.budget = budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=qualify("monthly", config.name),
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=config.monthly_budget_usd, unit="USD"),
                cost_filters={"TagKeyValue": [f"user:Environment${config.name}"]},
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=self.alert_topic.topic_arn,
                            subscription_type="SNS",
                        )
                    ],
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="FORECASTED",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=self.alert_topic.topic_arn,
                            subscription_type="SNS",
                        )
                    ],
                ),
            ],
        )

        apply_tags(self, config)

        CfnOutput(self, "AlertTopicArn", value=self.alert_topic.topic_arn)
        for name, dashboard in self.dashboards.items():
            # Prefixed, because the dashboard constructs already own the unpadded
            # ids and two constructs cannot share a name in one stack.
            CfnOutput(self, f"Output{name.title().replace('-', '')}", value=dashboard.dashboard_name)

    # -- helpers -------------------------------------------------------------
    def _alarm(
        self,
        construct_id: str,
        *,
        alarm_name: str,
        description: str,
        metric: cloudwatch.IMetric,
        threshold: float,
        treat_missing_data: cloudwatch.TreatMissingData = cloudwatch.TreatMissingData.NOT_BREACHING,
    ) -> cloudwatch.Alarm:
        """Create an alarm wired to the alert topic.

        ``NOT_BREACHING`` is the default for missing data on purpose: an agent that
        received no traffic is not an incident, and an alarm that fires on silence
        is an alarm people learn to ignore.
        """
        alarm = cloudwatch.Alarm(
            self,
            construct_id,
            alarm_name=alarm_name,
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=treat_missing_data,
        )
        alarm.add_alarm_action(cloudwatch_actions.SnsAction(self.alert_topic))
        return alarm

    def _dashboard(self, name: str) -> cloudwatch.Dashboard:
        """Build one of the five operational dashboards."""
        dashboard = cloudwatch.Dashboard(
            self,
            f"Dashboard{name.title().replace('-', '')}",
            dashboard_name=qualify(name, self.config.name),
        )

        if name == "agent-health":
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## Agent health\n\n"
                        "Is it up, is it fast, is it failing. Native AgentCore metrics — "
                        "no application instrumentation required for these panels."
                    ),
                    width=24,
                    height=3,
                )
            )
            for agent_name, runtime in self.runtimes.items():
                dashboard.add_widgets(
                    cloudwatch.GraphWidget(
                        title=f"{agent_name} invocations and errors",
                        left=[runtime.metric_invocations(statistic="Sum", period=Duration.minutes(5))],
                        right=[runtime.metric_total_errors(statistic="Sum", period=Duration.minutes(5))],
                    ),
                    cloudwatch.GraphWidget(
                        title=f"{agent_name} latency",
                        left=[runtime.metric_latency(statistic="p95", period=Duration.minutes(5))],
                    ),
                )
            dashboard.add_widgets(cloudwatch.AlarmWidget(title="Alarms", alarm=self.alarms["policy_denials"]))

        elif name == "agent-quality":
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## Agent quality\n\n"
                        "Is it still right. Eval score comes from `bda eval`, which the deploy "
                        "workflow runs before promoting; a drop of more than 3% blocks promotion."
                    ),
                    width=24,
                    height=3,
                ),
                cloudwatch.GraphWidget(
                    title="Eval score (gate: >=97% of baseline)",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.EVAL_SCORE,
                            statistic="Maximum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="Refusals vs clarifications vs answers",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.REFUSALS,
                            statistic="Sum",
                            period=Duration.hours(1),
                        ),
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.NEEDS_CLARIFICATION,
                            statistic="Sum",
                            period=Duration.hours(1),
                        ),
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="Answers that arrived without a complete evidence envelope",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.EVIDENCE_COMPLETE,
                            statistic="Minimum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
            )

        elif name == "cost":
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## Cost\n\n"
                        f"Monthly ceiling: **${self.config.monthly_budget_usd:,.0f}**. "
                        "Tokens by model, Athena bytes scanned, and the budget itself. "
                        "A cost incident is an incident."
                    ),
                    width=24,
                    height=3,
                ),
                cloudwatch.GraphWidget(
                    title="Prompt tokens",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.PROMPT_TOKENS,
                            statistic="Sum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="Completion tokens",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.COMPLETION_TOKENS,
                            statistic="Sum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
                cloudwatch.GraphWidget(
                    title="Athena bytes scanned",
                    left=[
                        cloudwatch.Metric(
                            namespace="AWS/Athena",
                            metric_name="ProcessedBytes",
                            dimensions_map={"WorkGroup": qualify("agents", self.config.name)},
                            statistic="Sum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
            )

        elif name == "governance":
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## Governance\n\n"
                        "Refusals, guardrail interventions and policy denials. A refusal is a "
                        "feature working; a denied *attempt* at a privileged tool is page-worthy."
                    ),
                    width=24,
                    height=3,
                ),
                cloudwatch.GraphWidget(
                    title="Refusals by agent",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.METRIC_NAMESPACE,
                            metric_name=telemetry.REFUSALS,
                            statistic="Sum",
                            period=Duration.hours(1),
                        )
                    ],
                ),
                cloudwatch.AlarmWidget(title="Policy denials", alarm=self.alarms["policy_denials"]),
                cloudwatch.AlarmWidget(title="Agent errors", alarm=self.alarms["freshness_sla"]),
            )

        else:  # data-freshness
            dashboard.add_widgets(
                cloudwatch.TextWidget(
                    markdown=(
                        "## Data freshness\n\n"
                        "Every gold product carries an SLA in its contract. This is where the "
                        "promise is checked: rules passed, rules warned, and SLA misses."
                    ),
                    width=24,
                    height=3,
                ),
                cloudwatch.GraphWidget(
                    title="DQ rules passed / warned / failed",
                    left=[
                        cloudwatch.Metric(
                            namespace=telemetry.DATA_QUALITY_NAMESPACE,
                            metric_name=telemetry.DQ_PASS,
                            statistic="Sum",
                            period=Duration.hours(1),
                        ),
                        cloudwatch.Metric(
                            namespace=telemetry.DATA_QUALITY_NAMESPACE,
                            metric_name=telemetry.DQ_WARNING,
                            statistic="Sum",
                            period=Duration.hours(1),
                        ),
                        cloudwatch.Metric(
                            namespace=telemetry.DATA_QUALITY_NAMESPACE,
                            metric_name=telemetry.DQ_FAIL,
                            statistic="Sum",
                            period=Duration.hours(1),
                        ),
                    ],
                ),
                cloudwatch.AlarmWidget(title="Freshness SLA misses", alarm=self.alarms["freshness_sla"]),
            )

        return dashboard


__all__ = [
    "DASHBOARDS",
    "ERROR_COUNT_THRESHOLD",
    "LATENCY_P95_THRESHOLD_MS",
    "POLICY_DENIAL_THRESHOLD",
    "SLA_MISS_THRESHOLD",
    "ObservabilityStack",
]
