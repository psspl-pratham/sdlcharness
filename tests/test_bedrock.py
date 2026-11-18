"""Bedrock contract tests use SDK stubs/scripted responses; no AWS calls or charges."""
import json
import sys
from types import SimpleNamespace

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import NoCredentialsError
from botocore.stub import ANY, Stubber
from fastapi.testclient import TestClient

from sdlc_harness.agents import AgentRuntime
from fixtures import FixtureGateway
from sdlc_harness.api import create_app
from sdlc_harness.cli import main
from sdlc_harness.models import Brief, FilePatch
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.providers import BedrockGateway, GatewayResponse, ProviderSettings
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store


def reply(text, stop_reason="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": stop_reason, "usage": {"inputTokens": 20, "outputTokens": 30, "totalTokens": 50},
            "metrics": {"latencyMs": 10}}


def sdk_client():
    # Explicit dummy credentials + Stubber ensure no metadata lookup or AWS HTTP call.
    return boto3.client("bedrock-runtime", region_name="us-east-1",
                        aws_access_key_id="testing", aws_secret_access_key="testing")


class ScriptedBedrockClient:
    """An offline stand-in for AWS used only to test orchestration and provider routing."""
    def __init__(self):
        self.calls = []

    def converse(self, **request):
        self.calls.append(request)
        prompt = request["messages"][0]["content"][0]["text"]
        payload = json.loads(prompt)
        fixture = FixtureGateway().complete(role="test", task=payload["task"], inputs=payload["inputs"],
                                         prompt=prompt, max_tokens=128, timeout=5)
        return reply(json.dumps(fixture))


def live_runner(tmp_path, client, **kwargs):
    gateway = BedrockGateway("test-model", "us-east-1", client=client)
    return Orchestrator(Store(tmp_path), gateway=gateway, **kwargs)


def test_settings_are_bedrock_only_and_explicit_values_override_environment(monkeypatch):
    monkeypatch.delenv("HARNESS_PROVIDER", raising=False)
    monkeypatch.delenv("HARNESS_SANDBOX", raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "private-test-token")
    assert ProviderSettings.from_env().provider == "bedrock"
    with pytest.raises(ValueError):
        ProviderSettings.from_env(provider="mock")
    monkeypatch.setenv("HARNESS_PROVIDER", "bedrock")
    monkeypatch.setenv("HARNESS_BEDROCK_MODEL_ID", "environment-model")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    settings = ProviderSettings.from_env(model_id="explicit-model", region="us-west-2")
    assert settings.model_id == "explicit-model" and settings.region == "us-west-2"
    assert settings.sandbox == "docker"
    assert "private-test-token" not in settings.model_dump_json()


@pytest.mark.parametrize("config,message", [
    ({"region": "us-east-1"}, "HARNESS_BEDROCK_MODEL_ID"),
    ({"model_id": "model"}, "AWS_REGION"),
    ({"model_id": "model", "region": "us-east-1", "sandbox": "local"}, "requires HARNESS_SANDBOX=docker"),
])
def test_bad_bedrock_configuration_fails_early(config, message):
    with pytest.raises(ValueError, match=message):
        ProviderSettings(provider="bedrock", **config)


def test_sdk_converse_request_schema_and_usage_audit():
    client = sdk_client()
    gateway = BedrockGateway("test-model", "us-east-1", client=client, max_tokens=256)
    events = []
    content = {"name": "Books", "problem": "Track books", "users": ["Readers"], "entities": ["Book"], "constraints": []}
    expected = {"modelId": "test-model", "system": ANY,
                "messages": ANY, "inferenceConfig": {"maxTokens": 256}}
    with Stubber(client) as stubber:
        stubber.add_response("converse", reply(json.dumps(content)), expected)
        result = AgentRuntime(gateway, lambda kind, **data: events.append(data) if kind == "agent.call" else None).produce(
            "product_manager", "intake", {"idea": "A book library"}, Brief, "Use Book entities",
        )
        stubber.assert_no_pending_responses()
    assert result.entities == ["Book"]
    assert events[0]["provider"] == "bedrock" and events[0]["model_id"] == "test-model"
    assert events[0]["tokens"] == 50 and events[0]["usage"]["inputTokens"] == 20


def test_invalid_json_retries_once_with_validation_feedback_and_logs_both_usages():
    client = ScriptedBedrockClient()
    requests = []

    def converse(**request):
        requests.append(request)
        return reply("broken JSON" if len(requests) == 1 else json.dumps({
            "name": "Books", "problem": "Track books", "users": [], "entities": ["Book"], "constraints": [],
        }))

    client.converse = converse
    events, reservations = [], []
    runtime = AgentRuntime(BedrockGateway("model", "us-east-1", client=client),
                           lambda kind, **data: events.append(data) if kind == "agent.call" else None,
                           reserve_call=lambda role, task: reservations.append((role, task)))
    assert runtime.produce("product_manager", "intake", {"idea": "Books"}, Brief).name == "Books"
    assert len(reservations) == 2
    assert "Validation error" in requests[1]["messages"][0]["content"][0]["text"]
    assert [e["outcome"] for e in events] == ["invalid", "valid"]
    assert [e["tokens"] for e in events] == [50, 50]


def test_provider_errors_do_not_leak_response_bodies_or_credentials():
    client = sdk_client()
    with Stubber(client) as stubber:
        stubber.add_client_error("converse", service_error_code="AccessDeniedException",
                                 service_message="private-token-value-must-not-leak", http_status_code=403)
        with pytest.raises(RuntimeError) as error:
            BedrockGateway("model", "us-east-1", client=client).complete(
                role="test", task="test", inputs={}, prompt="JSON please", max_tokens=64, timeout=5,
            )
    assert "AccessDeniedException" in str(error.value)
    assert "private-token-value" not in str(error.value)


def test_missing_credentials_have_an_actionable_error(monkeypatch):
    gateway = BedrockGateway("model", "us-east-1")

    def missing():
        raise NoCredentialsError()

    monkeypatch.setattr(gateway, "_get_client", missing)
    with pytest.raises(RuntimeError, match="AWS_BEARER_TOKEN_BEDROCK"):
        gateway.complete(role="test", task="test", inputs={}, prompt="JSON", max_tokens=64, timeout=5)


def test_truncated_output_is_not_accepted_as_a_valid_artifact():
    client = SimpleNamespace(converse=lambda **kwargs: reply('{"name":"Looks plausible"}', "max_tokens"))
    events = []
    with pytest.raises(RuntimeError, match="max_tokens"):
        AgentRuntime(BedrockGateway("model", "us-east-1", client=client),
                     lambda kind, **data: events.append(data) if kind == "agent.call" else None).produce("product_manager", "intake", {}, Brief)
    assert events[0]["outcome"] == "failed" and events[0]["tokens"] == 50


def patch_reply(patch, stop_reason="tool_use"):
    response = reply("", stop_reason)
    response['output']['message']['content'] = [{
        'toolUse': {'toolUseId': 'tooluse_offline_test', 'name': 'submit_patch', 'input': patch},
    }]
    return response


def test_nova_patch_uses_forced_tool_and_preserves_code_quotes_and_newlines():
    client = sdk_client()
    gateway = BedrockGateway('apac.amazon.nova-lite-v1:0', 'ap-south-1', client=client, max_tokens=512)
    patch = {'files': {'app.py': 'message = "a \\\"quoted\\\" value"\nprint(message)\n'}, 'notes': 'Real code string'}
    expected = {'modelId': gateway.model_id, 'system': ANY, 'messages': ANY,
                'toolConfig': {'toolChoice': {'tool': {'name': 'submit_patch'}}, 'tools': ANY},
                'inferenceConfig': {'maxTokens': 512, 'temperature': 0}}
    events = []
    with Stubber(client) as stubber:
        stubber.add_response('converse', patch_reply(patch), expected)
        result = AgentRuntime(gateway, lambda kind, **data: events.append(data) if kind == 'agent.call' else None).produce(
            'qa', 'repair:T1', {'idea': 'Test code patch'}, FilePatch)
        stubber.assert_no_pending_responses()
    assert result.files == patch['files']
    assert events[-1]['response_format'] == 'tool_arguments'
    assert events[-1]['tokens'] == 50


def test_nova_tool_arguments_still_receive_full_artifact_validation():
    requests = []

    def converse(**request):
        requests.append(request)
        schema = request['toolConfig']['tools'][0]['toolSpec']['inputSchema']['json']
        assert set(schema) == {'type', 'properties', 'required'}
        return patch_reply({'files': []} if len(requests) == 1 else {'files': {'app.py': 'print("ok")\n'}})

    gateway = BedrockGateway('amazon.nova-lite-v1:0', 'ap-south-1', client=SimpleNamespace(converse=converse))
    events = []
    result = AgentRuntime(gateway, lambda kind, **data: events.append(data) if kind == 'agent.call' else None).produce(
        'developer', 'build:T1', {}, FilePatch)
    assert result.files == {'app.py': 'print("ok")\n'}
    assert [e['outcome'] for e in events] == ['invalid', 'valid']
    assert all(e['response_format'] == 'tool_arguments' for e in events)


@pytest.mark.parametrize('mode', ['missing', 'ambiguous', 'truncated'])
def test_nova_missing_ambiguous_or_truncated_tool_is_rejected_with_usage(mode):
    response = patch_reply({'files': {'app.py': 'print("ok")'}})
    if mode == 'missing':
        response = reply('A patch as prose is not accepted')
    elif mode == 'ambiguous':
        response['output']['message']['content'] *= 2
    else:
        response['stopReason'] = 'max_tokens'
    gateway = BedrockGateway('amazon.nova-lite-v1:0', 'ap-south-1',
                             client=SimpleNamespace(converse=lambda **kw: response))
    events = []
    with pytest.raises(RuntimeError):
        AgentRuntime(gateway, lambda kind, **data: events.append(data) if kind == 'agent.call' else None).produce(
            'qa', 'repair:T1', {}, FilePatch)
    assert events[-1]['outcome'] == 'failed' and events[-1]['tokens'] == 50


def test_client_creation_uses_sdk_credentials_and_explicit_timeout_without_network(monkeypatch):
    captured = {}

    def make_client(service, **kwargs):
        captured.update(service=service, **kwargs)
        return object()

    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(client=make_client))
    gateway = BedrockGateway("model", "us-east-1", timeout=90)
    assert gateway._get_client() is gateway._get_client()
    assert captured["service"] == "bedrock-runtime"
    assert captured["config"].read_timeout == 90
    assert captured["config"].retries["total_max_attempts"] == 1
    assert "aws_secret_access_key" not in captured


def test_live_project_planning_uses_saved_provider_and_measured_usage(tmp_path):
    client = ScriptedBedrockClient()
    runner = live_runner(tmp_path, client)
    project = runner.create("A different idea", "planning-only")
    project = runner.run(project.id)
    assert project.status == "awaiting_approval"
    assert project.provider.name == "bedrock" and project.provider.model_id == "test-model"
    assert project.model_calls == len(client.calls) == 4
    assert all(e["data"]["tokens"] == 50 for e in runner.store.events(project.id) if e["type"] == "agent.call")
    assert "Do not substitute a generic sample product" in client.calls[0]["messages"][0]["content"][0]["text"]


def test_model_call_limit_persists_across_resume_and_rerun(tmp_path):
    client = ScriptedBedrockClient()
    runner = live_runner(tmp_path, client, max_model_calls=2)
    project = runner.create("Small idea", "planning-only")
    project = runner.run(project.id)
    assert project.status == "failed" and "model-call limit" in project.error
    assert project.model_calls == len(client.calls) == 2
    restarted = live_runner(tmp_path, client)
    restarted.rerun(project.id, "architecture")
    project = restarted.run(project.id)
    assert project.status == "failed" and project.model_calls == 2
    assert len(client.calls) == 2


def test_existing_mock_projects_are_readable_but_cannot_run_on_bedrock(tmp_path):
    mock_runner = Orchestrator(Store(tmp_path), gateway=FixtureGateway(), sandbox=Sandbox("local"))
    project = mock_runner.create("Fixture", "planning-only")
    mock_runner.run(project.id)
    state_path = mock_runner.store.directory(project.id) / "state.json"
    state = json.loads(state_path.read_text())
    state.pop("provider")  # Compatibility with ledgers created before provider configuration existed.
    state_path.write_text(json.dumps(state))
    client = ScriptedBedrockClient()
    runner = live_runner(tmp_path, client)
    saved = runner.store.load(project.id)
    assert saved.provider.name == "mock"
    with pytest.raises(ValueError, match="Historical mock projects are read-only"):
        runner.gateway_for(saved)
    assert not client.calls


def test_changed_live_model_and_local_deploy_are_rejected(tmp_path):
    client = ScriptedBedrockClient()
    runner = live_runner(tmp_path, client)
    project = runner.create("Idea", "planning-only")
    runner.gateway.model_id = "different-model"
    with pytest.raises(ValueError, match="differs"):
        runner.run(project.id)
    with pytest.raises(ValueError, match="Docker deploy target"):
        runner.create("Idea", "autopilot")
    assert not client.calls


def test_doctor_displays_configuration_without_aws_calls_or_secrets(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "not-a-real-key-do-not-print")
    monkeypatch.setattr("sdlc_harness.providers.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    result = ProviderSettings(provider="bedrock", region="us-east-1", model_id="model").doctor()
    assert result["aws_requests_made"] == 0
    assert "not-a-real-key" not in json.dumps(result)
    assert result["checks"]["docker_available"]


def test_check_model_makes_one_small_request_only_when_explicitly_invoked(tmp_path, monkeypatch, capsys):
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return GatewayResponse('{"ok":true}', {"totalTokens": 12})

    gateway = SimpleNamespace(model_id="test-model", region="us-east-1", complete=complete)
    monkeypatch.setattr(ProviderSettings, "gateway", lambda self: gateway)
    monkeypatch.setattr(sys, "argv", ["harness", "--data-dir", str(tmp_path), "--provider", "bedrock",
                                     "--model-id", "test-model", "--region", "us-east-1", "check-model"])
    main()
    assert len(calls) == 1 and calls[0]["max_tokens"] == 64
    assert json.loads(capsys.readouterr().out)["usage"]["totalTokens"] == 12


def test_api_exposes_active_provider_and_saves_it_on_new_projects(tmp_path, monkeypatch):
    client = ScriptedBedrockClient()
    gateway = BedrockGateway("test-model", "us-east-1", client=client)
    monkeypatch.setattr(ProviderSettings, "gateway", lambda self: gateway)
    settings = ProviderSettings(provider="bedrock", model_id="test-model", region="us-east-1")
    with TestClient(create_app(tmp_path, settings=settings)) as api:
        health = api.get("/health").json()
        assert health["provider"] == "bedrock" and health["model_id"] == "test-model"
        response = api.post("/api/projects", headers={"X-Harness-Request": "1"},
                            json={"idea": "A book service", "blueprint": "planning-only"})
        assert response.status_code == 202
        assert response.json()["provider"]["name"] == "bedrock"
