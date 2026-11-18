"""Regressions for the observed Nova Lite intake envelope and duplicate retry loop."""
import json

import pytest

from fixtures import FixtureGateway
from sdlc_harness.agents import AgentOutputError, AgentRuntime
from sdlc_harness.models import Brief
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store


IDEA = "Make a notes app with CRUD and a maximum of 3 pinned notes"
BRIEF = {
    "name": "Notes App", "problem": "Manage personal notes", "users": ["Note taker"],
    "entities": ["Note"], "constraints": ["A maximum of 3 notes may be pinned"],
}


class Responses(FixtureGateway):
    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def runtime(gateway, events):
    return AgentRuntime(gateway, lambda kind, **data: events.append((kind, data)))


@pytest.mark.parametrize("as_json", [True, False])
def test_single_brief_envelope_is_validated_without_spending_a_retry(as_json):
    wrapped = {"brief": BRIEF}
    gateway = Responses(json.dumps(wrapped) if as_json else wrapped)
    events = []
    result = runtime(gateway, events).produce("product_manager", "intake", {"idea": IDEA}, Brief)
    assert result.model_dump() == BRIEF
    assert len(gateway.calls) == 1
    assert events[-1][1]["outcome"] == "valid"
    assert events[-1][1]["unwrapped_from"] == "brief"


def test_multi_stage_response_is_rejected_and_repaired_with_root_contract():
    gateway = Responses({"brief": BRIEF, "requirements_txt": "private-output-value"}, BRIEF)
    events = []
    result = runtime(gateway, events).produce("product_manager", "intake", {"idea": IDEA}, Brief)
    assert result.model_dump() == BRIEF
    invalid = next(data for kind, data in events if kind == "agent.call")
    assert invalid["outcome"] == "invalid" and invalid["retrying"] is True
    assert {"path": "name", "type": "missing"} in invalid["validation_errors"]
    assert {"path": "brief", "type": "extra_forbidden"} in invalid["validation_errors"]
    repair = json.loads(gateway.calls[1]["prompt"])
    assert "Expected root fields: name, problem, users, entities, constraints" in repair["validation_feedback"]["summary"]
    assert repair["inputs"]["idea"] == IDEA
    assert "private-output-value" not in json.dumps(events) + gateway.calls[1]["prompt"]


def test_envelope_cannot_hide_bad_fields_or_extra_fields_inside_brief():
    gateway = Responses({"brief": {**BRIEF, "constraints": "private-invalid-value", "extra": True}})
    events = []
    with pytest.raises(AgentOutputError, match="Stopped after 2 invalid responses") as exc:
        runtime(gateway, events).produce("product_manager", "intake", {"idea": IDEA}, Brief)
    calls = [data for kind, data in events if kind == "agent.call"]
    assert len(gateway.calls) == 2
    assert [e["retrying"] for e in calls] == [True, False]
    assert {"path": "constraints", "type": "list_type"} in calls[-1]["validation_errors"]
    assert {"path": "extra", "type": "extra_forbidden"} in calls[-1]["validation_errors"]
    assert "private-invalid-value" not in str(exc.value) + json.dumps(events) + gateway.calls[1]["prompt"]


def test_malformed_json_reports_location_without_copying_the_response():
    gateway = Responses('{"private-output-value":')
    events = []
    with pytest.raises(AgentOutputError, match="Invalid JSON at line 1"):
        runtime(gateway, events).produce("product_manager", "intake", {"idea": IDEA}, Brief)
    assert "private-output-value" not in json.dumps(events) + gateway.calls[1]["prompt"]


def test_validation_exhaustion_does_not_multiply_stage_retries_or_repeat_on_resume(tmp_path):
    gateway = Responses({"brief": {"name": "Incomplete"}})
    runner = Orchestrator(Store(tmp_path), gateway, sandbox=Sandbox("local"))
    project = runner.create(IDEA, "planning-only")
    result = runner.run(project.id)
    assert result.status == "failed" and result.model_calls == 2
    assert result.stages["intake"].attempts == 1
    assert result.stages["requirements"].status == "pending" and not result.artifacts
    failure = next(e for e in runner.store.events(project.id) if e["type"] == "stage.failed")
    assert failure["data"]["retrying"] is False
    runner.run(project.id)
    assert len(gateway.calls) == 2
    # An explicit user retry resets stage attempts but keeps the paid-call ledger.
    runner.rerun(project.id, "intake")
    result = runner.run(project.id)
    assert result.model_calls == 4 and len(gateway.calls) == 4


def test_intake_prompt_only_requests_a_brief_and_preserves_user_constraints():
    gateway = Responses(BRIEF)
    runtime(gateway, []).produce("product_manager", "intake", {"idea": IDEA}, Brief)
    prompt = json.loads(gateway.calls[0]["prompt"])
    assert prompt["inputs"]["idea"] == IDEA
    assert "directly at the root" in prompt["response_contract"]
    assert "Preserve explicit limits" in prompt["stage_scope"]
    assert "requirements.txt" not in prompt["persona"]
    assert "HTMLResponse" not in prompt["persona"]
