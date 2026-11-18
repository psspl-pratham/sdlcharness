"""Bedrock configuration. Credentials stay in the AWS SDK credential chain."""
import importlib.util
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, model_validator

from .models import Model, ProviderInfo


def load_environment():
    """Load this working directory's .env without replacing terminal settings."""
    load_dotenv(Path.cwd() / ".env", override=False, encoding="utf-8-sig", interpolate=False)
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK") and os.environ.get("BEDROCK_API_KEY"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = os.environ["BEDROCK_API_KEY"]


@dataclass
class GatewayResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "end_turn"
    artifact: dict | None = None


def provider_info(gateway) -> ProviderInfo:
    return ProviderInfo(name=gateway.name, model_id=getattr(gateway, "model_id", None),
                        region=getattr(gateway, "region", None))


class ProviderSettings(Model):
    provider: Literal["bedrock"] = "bedrock"
    model_id: str | None = None
    region: str | None = None
    sandbox: Literal["local", "docker"] | None = None
    max_output_tokens: int = Field(default=4096, ge=128, le=16384)
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_model_calls: int = Field(default=40, ge=1, le=200)

    @model_validator(mode="after")
    def validate_bedrock(self):
        if self.provider == "bedrock":
            if not self.model_id or not self.model_id.strip():
                raise ValueError("Set HARNESS_BEDROCK_MODEL_ID (model ID or inference profile ID/ARN from Bedrock)")
            if not self.region or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region):
                raise ValueError("Set AWS_REGION to the region where your Bedrock model is available")
            if self.sandbox == "local":
                raise ValueError("Bedrock-generated code requires HARNESS_SANDBOX=docker")
        if self.sandbox is None:
            self.sandbox = "docker" if self.provider == "bedrock" else "local"
        return self

    @classmethod
    def from_env(cls, *, provider=None, model_id=None, region=None, sandbox=None):
        load_environment()
        return cls(
            provider=provider or os.environ.get("HARNESS_PROVIDER", "bedrock"),
            model_id=model_id or os.environ.get("HARNESS_BEDROCK_MODEL_ID") or None,
            region=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None,
            sandbox=sandbox or os.environ.get("HARNESS_SANDBOX") or None,
            max_output_tokens=os.environ.get("HARNESS_MAX_OUTPUT_TOKENS", "4096"),
            timeout_seconds=os.environ.get("HARNESS_MODEL_TIMEOUT_SECONDS", "60"),
            max_model_calls=os.environ.get("HARNESS_MAX_MODEL_CALLS", "40"),
        )

    def gateway(self):
        if importlib.util.find_spec("boto3") is None:
            raise ValueError("Install Bedrock support: .venv/bin/python -m pip install -e '.[bedrock]'")
        return BedrockGateway(self.model_id, self.region, max_tokens=self.max_output_tokens,
                              timeout=self.timeout_seconds)

    def runner(self, store):
        from .orchestrator import Orchestrator
        from .sandbox import Sandbox
        return Orchestrator(store, gateway=self.gateway(), sandbox=Sandbox(self.sandbox),
                            max_model_calls=self.max_model_calls)

    def doctor(self):
        """No AWS requests: inspect local configuration and Docker prerequisites only."""
        checks = {"bedrock_sdk_installed": importlib.util.find_spec("boto3") is not None}
        if self.sandbox == "docker":
            for name, args in [
                ("docker_available", ["docker", "info", "--format", "{{.ServerVersion}}"]),
                ("sandbox_image_available", ["docker", "image", "inspect", "sdlc-harness-sandbox:local"]),
            ]:
                try:
                    checks[name] = subprocess.run(args, capture_output=True, timeout=10).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    checks[name] = False
        auth = ("Bedrock token environment variable is set" if os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                else "AWS profile is selected" if os.environ.get("AWS_PROFILE")
                else "AWS environment credentials are set" if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")
                else "Default AWS SDK credential chain; authentication has not been checked")
        return {"settings": self.model_dump(), "checks": checks,
                "authentication": auth,
                "aws_requests_made": 0,
                "ready_for_connection_test": self.provider == "bedrock" and checks["bedrock_sdk_installed"]}


class BedrockGateway:
    name = "bedrock"

    def __init__(self, model_id: str, region: str, *, max_tokens=4096, timeout=60, client=None):
        self.model_id, self.region = model_id, region
        self.max_tokens, self.timeout = max_tokens, timeout
        self._client, self._client_lock = client, threading.Lock()

    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                import boto3
                from botocore.config import Config
                # SDK supports AWS_BEARER_TOKEN_BEDROCK and the standard AWS credential chain.
                self._client = boto3.Session().client(
                    "bedrock-runtime", region_name=self.region,
                    config=Config(read_timeout=self.timeout, connect_timeout=min(10, self.timeout),
                                  retries={"mode": "standard", "total_max_attempts": 1}),
                )
            return self._client

    def complete(self, *, role, task, inputs, prompt, max_tokens, timeout):
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        if len(prompt.encode()) > 100_000:
            raise ValueError("Model prompt exceeds the 100 KB POC context budget")
        tool_config = None
        # Nova supports forced tool choice. Use it for code patches, whose nested
        # quotes/newlines are fragile when emitted as a free-form JSON text string.
        if role in {"developer", "qa"} and "amazon.nova-" in self.model_id:
            schema = json.loads(prompt)["schema"]
            tool_config = {
                "toolChoice": {"tool": {"name": "submit_patch"}},
                "tools": [{"toolSpec": {
                    "name": "submit_patch", "description": "Submit the changed files as complete replacement contents and brief notes. This records a patch for validation; it does not execute commands.",
                    # Nova accepts only these three fields at the schema root.
                    "inputSchema": {"json": {k: schema[k] for k in ("type", "properties", "required") if k in schema}},
                }}],
            }
        system = "You are a software delivery agent executing ONE stage of a workflow. Follow the response_contract, stage_scope, JSON Schema, role, inputs, and feedback in the user message. The schema describes the response object itself: put its fields directly at the root, with no artifact-name wrapper or additional fields. Do not perform later stages. Never request or output credentials. Do not invent test or deployment results. "
        system += ("Call submit_patch exactly once with the actual file contents as tool arguments. Do not return JSON as text or encode the arguments inside a string."
                   if tool_config else "Return exactly one JSON object, with no Markdown fences or commentary.")
        try:
            response = self._get_client().converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": min(max_tokens, self.max_tokens), **({"temperature": 0} if tool_config else {})},
                **({"toolConfig": tool_config} if tool_config else {}),
            )
        except NoCredentialsError as error:
            raise RuntimeError("No AWS credentials found. Set AWS_BEARER_TOKEN_BEDROCK or configure an AWS profile/temporary credentials.") from error
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "Unknown")
            messages = {
                "AccessDeniedException": "Check IAM bedrock:InvokeModel permission, model access, region, and account eligibility.",
                "ValidationException": "Check the model/inference-profile ID, region, Converse support, and token limit.",
                "ResourceNotFoundException": "The model or inference profile was not found in the configured region.",
                "ThrottlingException": "The model quota or request rate was exceeded. Check Bedrock Service Quotas.",
                "ExpiredTokenException": "Refresh your AWS session or Bedrock API key.",
            }
            # Never copy provider response bodies or request headers into logs/ledger errors.
            safe_code = re.sub(r"[^A-Za-z0-9_]", "", str(code))[:80]
            raise RuntimeError(f"Bedrock {safe_code}: {messages.get(code, 'The request failed; inspect the AWS account/model configuration. ')}") from error
        except BotoCoreError as error:
            raise RuntimeError(f"Bedrock connection/authentication error ({type(error).__name__}); check AWS credentials, region, network, and timeout.") from error
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in content)
        usage = {key: value for key, value in response.get("usage", {}).items() if isinstance(value, int)}
        if tool_config:
            calls = [block["toolUse"] for block in content if "toolUse" in block]
            if len(calls) == 1 and calls[0].get("name") == "submit_patch" and isinstance(calls[0].get("input"), dict):
                return GatewayResponse(text="", usage=usage, stop_reason=response.get("stopReason", "unknown"), artifact=calls[0]["input"])
            return GatewayResponse(text="", usage=usage, stop_reason="missing_or_ambiguous_patch_tool")
        return GatewayResponse(text=text, usage=usage, stop_reason=response.get("stopReason", "unknown"))
