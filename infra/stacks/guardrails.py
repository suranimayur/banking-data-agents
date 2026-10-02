"""The guardrails stack: one versioned Bedrock Guardrail, applied at the edges.

A guardrail is not a content filter bolted onto a chatbot. Here it is the control
that stops the platform's *own* refusals from being negotiable: the agent layer
refuses "rank customers by credit risk for marketing" because the data contract
says so, and the guardrail is the second, independent line of defence that catches
the PII or the model creativity that the contract cannot see.

Three decisions are encoded here.

**Filters apply to input and output, not to intermediate steps.** Applying a
guardrail to every tool result doubles cost and latency and, worse, anonymises the
numbers the agent is reasoning over. The user's question and the final answer are
the two places where a guardrail belongs.

**PII is anonymised, identifiers are blocked.** Names, emails and phone numbers
are masked so an analyst can still work; card numbers, Aadhaar and PAN are
blocked outright because there is no legitimate reason for them to appear in an
answer at all.

**The guardrail is versioned and its version is an output.** A guardrail id alone
means "whatever the rule is today"; pinning the version is what makes an old
answer reproducible during an audit, which is the entire point of keeping
evidence envelopes.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrock as bedrock
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import apply_tags
from infra.stacks.storage_lake import StorageLakeStack

#: Content filters. ``PROMPT_ATTACK`` has no output mode — an output cannot be a
#: prompt injection — so it is expressed with output_strength NONE.
CONTENT_FILTERS: tuple[tuple[str, str, str], ...] = (
    ("PROMPT_ATTACK", "HIGH", "NONE"),
    ("HATE", "HIGH", "HIGH"),
    ("INSULTS", "HIGH", "HIGH"),
    ("SEXUAL", "HIGH", "HIGH"),
    ("VIOLENCE", "HIGH", "HIGH"),
    ("MISCONDUCT", "HIGH", "HIGH"),
)

#: Topics the agent must not be talked into reasoning about. Each carries
#: definitions and examples because a denied topic without examples is a filter
#: that only fires on the phrasing its author happened to think of.
DENIED_TOPICS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "investment_advice",
        "Recommending investments, securities or financial products to a customer.",
        (
            "should I put my savings into this fund",
            "which stock should this customer buy",
        ),
    ),
    (
        "automated_credit_decision",
        "Producing a final approve or decline credit decision without a human underwriter.",
        (
            "approve this loan automatically",
            "decline this customer's application",
        ),
    ),
    (
        "individual_marketing_on_risk",
        "Selecting individual customers for marketing based on credit or risk attributes.",
        (
            "find customers with bad credit to sell loans to",
            "target the high default risk segment with credit cards",
        ),
    ),
    (
        "customer_profiling_evasion",
        "Attempts to re-identify a customer after identifiers have been masked.",
        (
            "which customer has this account number",
            "show me the raw customer record",
        ),
    ),
)

#: PII that gets masked rather than blocked: the analyst still needs to see that
#: *some* contact detail exists.
PII_ANONYMIZE: tuple[str, ...] = (
    "NAME",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "IP_ADDRESS",
)

#: Identifiers that are blocked outright. An answer has no legitimate need to
#: contain one of these, so the right response is to refuse, not to mask.
#:
#: Only entity types Bedrock actually supports appear here. Aadhaar and PAN are
#: absent from that list, which is why they are handled as regexes below rather
#: than being listed optimistically and silently ignored by the service.
PII_BLOCK: tuple[str, ...] = (
    "CREDIT_DEBIT_CARD_NUMBER",
    "CREDIT_DEBIT_CARD_CVV",
    "CREDIT_DEBIT_CARD_EXPIRY",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "SWIFT_CODE",
    "PIN",
)

#: Regex patterns for identifiers the managed PII detector does not cover: the two
#: Indian statutory identifiers a bank in this market must handle, plus our own
#: internal ids (``CUST-0000123``, ``ACCT-0000042``), which must never leave the
#: platform in an answer body.
INTERNAL_IDENTIFIER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "aadhaar_number",
        r"\b[2-9]\d{3}\s?\d{4}\s?\d{4}\b",
        "Aadhaar number. Not an entity type Bedrock supports, so it is matched by pattern.",
    ),
    (
        "pan_number",
        r"\b[A-Z]{5}\d{4}[A-Z]\b",
        "Indian PAN. Not an entity type Bedrock supports, so it is matched by pattern.",
    ),
    (
        "internal_customer_id",
        r"\bCUST-\d{7}\b",
        "Internal synthetic customer identifier. Never returned to a user; query by aggregate instead.",
    ),
    (
        "internal_account_id",
        r"\bACCT-\d{7}\b",
        "Internal synthetic account identifier. Never returned to a user.",
    ),
)


class GuardrailsStack(Stack):
    """A versioned Bedrock Guardrail plus the wiring that makes it usable."""

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
            description=f"{config.project} Bedrock guardrail ({config.name})",
            **kwargs,
        )
        self.config = config

        self.guardrail = bedrock.CfnGuardrail(
            self,
            "AgentGuardrail",
            name=qualify("agent", config.name),
            description=f"{config.project} input/output guardrail for analyst-facing answers ({config.name})",
            blocked_input_messaging=(
                "This question cannot be handled here. It asks for investment advice, a credit "
                "decision, or an individual customer's data. Ask a data product question instead, "
                "or route it to a human underwriter."
            ),
            blocked_outputs_messaging=(
                "The answer was withheld because it contained a prohibited topic or a sensitive "
                "identifier. The evidence envelope for this trace records why."
            ),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=filter_type,
                        input_strength=input_strength,
                        output_strength=output_strength,
                    )
                    for filter_type, input_strength, output_strength in CONTENT_FILTERS
                ]
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name=name,
                        definition=definition,
                        type="DENY",
                        examples=list(examples),
                    )
                    for name, definition, examples in DENIED_TOPICS
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=entity, action="ANONYMIZE")
                    for entity in PII_ANONYMIZE
                ]
                + [bedrock.CfnGuardrail.PiiEntityConfigProperty(type=entity, action="BLOCK") for entity in PII_BLOCK],
                regexes_config=[
                    bedrock.CfnGuardrail.RegexConfigProperty(
                        name=name,
                        pattern=pattern,
                        action="BLOCK",
                        description=description,
                    )
                    for name, pattern, description in INTERNAL_IDENTIFIER_PATTERNS
                ],
            ),
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[bedrock.CfnGuardrail.ManagedWordsConfigProperty(type="PROFANITY")]
            ),
            kms_key_arn=storage.key.key_arn,
            tags=[{"key": "Environment", "value": config.name}],
        )

        # A published version is immutable. Deployments point at a version, never at
        # "the guardrail", so a rule change is a new version and an old answer stays
        # explainable.
        self.guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "AgentGuardrailV1",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
            description=f"{config.project} guardrail v1 ({config.name})",
        )

        apply_tags(self, config)

        CfnOutput(self, "GuardrailId", value=self.guardrail.attr_guardrail_id)
        CfnOutput(self, "GuardrailVersion", value=self.guardrail_version.attr_version)


__all__ = [
    "CONTENT_FILTERS",
    "DENIED_TOPICS",
    "INTERNAL_IDENTIFIER_PATTERNS",
    "PII_ANONYMIZE",
    "PII_BLOCK",
    "GuardrailsStack",
]
