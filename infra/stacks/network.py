"""The network stack: an isolated VPC with no route to the internet.

**No NAT gateway is the design, not an oversight.** A NAT gateway is the single
most expensive line item in most small AWS accounts and it exists to let private
subnets reach the internet. Nothing here needs to: the agents call S3, DynamoDB,
KMS, Glue, Athena, Bedrock, Secrets Manager and the container registry, and every
one of those has a VPC endpoint. Reaching them without a NAT means a compromised
agent container with no egress path to exfiltrate to.

**Gateway endpoints for S3 and DynamoDB, interface endpoints for the rest.**
Gateway endpoints are free and are the reason the lake can be read at volume
without paying per-GB for it; interface endpoints charge hourly, so only the
services that the workload genuinely calls are listed.
"""

from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from infra.config import StageConfig, qualify
from infra.stacks.common import apply_tags

#: Interface endpoints the platform actually uses. Each one is here because some
#: running component calls that service from inside the VPC; this list is a
#: statement about the architecture and should be read as one.
INTERFACE_ENDPOINTS: dict[str, ec2.InterfaceVpcEndpointAwsService] = {
    "Kms": ec2.InterfaceVpcEndpointAwsService.KMS,
    "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    "Glue": ec2.InterfaceVpcEndpointAwsService.GLUE,
    "Athena": ec2.InterfaceVpcEndpointAwsService.ATHENA,
    "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
    "AgentCore": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
    "AgentCoreGateway": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE_GATEWAY,
    "Ecr": ec2.InterfaceVpcEndpointAwsService.ECR,
    "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    "CloudWatchLogs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    "CloudWatchMonitoring": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
}


class NetworkStack(Stack):
    """A private-by-default VPC sized for two availability zones."""

    def __init__(self, scope: Construct, construct_id: str, *, config: StageConfig, **kwargs) -> None:
        super().__init__(
            scope,
            construct_id,
            description=f"{config.project} network ({config.name}) - isolated VPC, no NAT",
            **kwargs,
        )
        self.config = config

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=qualify("vpc", config.name),
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
            max_azs=2,
            # Zero, explicitly. See the module docstring.
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="agents",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
            gateway_endpoints={
                "S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3),
                "DynamoDB": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.DYNAMODB),
            },
            enable_dns_hostnames=True,
            enable_dns_support=True,
        )

        for name, service in INTERFACE_ENDPOINTS.items():
            self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                # Private DNS is what makes the endpoint transparent to boto3: the
                # SDK resolves the public hostname and lands on the endpoint.
                private_dns_enabled=True,
            )

        apply_tags(self, config)


__all__ = ["INTERFACE_ENDPOINTS", "NetworkStack"]
