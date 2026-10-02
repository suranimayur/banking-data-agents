"""One module per logical system.

Reading order, if you are new to the repository, is: ``storage_lake`` (what data is
and where it lives), ``catalog`` (how it is governed), ``identity`` (who may touch
it), then ``agent_runtime`` (what runs), then the rest.

Every stack takes a :class:`infra.config.StageConfig` and reads its environment
differences from that object. No stack branches on the environment name.
"""

from __future__ import annotations

from infra.stacks.agent_runtime import AGENTS, AgentRuntimeStack
from infra.stacks.catalog import CatalogStack
from infra.stacks.common import GOVERNED_ZONES
from infra.stacks.gateway import GatewayStack
from infra.stacks.guardrails import GuardrailsStack
from infra.stacks.identity import ACCESS_GROUPS, IdentityStack
from infra.stacks.network import NetworkStack
from infra.stacks.observability import DASHBOARDS, ObservabilityStack
from infra.stacks.pipelines import PipelinesStack
from infra.stacks.storage_lake import ZONE_BUCKETS, StorageLakeStack
from infra.stacks.ui import ApiStack, ConsoleStack

__all__ = [
    "ACCESS_GROUPS",
    "AGENTS",
    "DASHBOARDS",
    "GOVERNED_ZONES",
    "ZONE_BUCKETS",
    "AgentRuntimeStack",
    "ApiStack",
    "CatalogStack",
    "ConsoleStack",
    "GatewayStack",
    "GuardrailsStack",
    "IdentityStack",
    "NetworkStack",
    "ObservabilityStack",
    "PipelinesStack",
    "StorageLakeStack",
]
