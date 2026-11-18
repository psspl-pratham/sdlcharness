import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fixtures import FixtureGateway
from sdlc_harness.agents import AgentRuntime
from sdlc_harness.api import create_app
from sdlc_harness.blueprints import load_blueprint, validate
from sdlc_harness.models import Brief
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.providers import ProviderSettings
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store
from sdlc_harness.workspace import Workspace


BROKEN_TEST = '''from fastapi.testclient import TestClient
from app import app
client = TestClient(app)

def test_get_created_item():
    local_items = {1: {"id": 1, "title": "Created item"}}
    client.app.dependency_overrides[lambda: local_items] = lambda: local_items
    response = client.get("/items/1")
    assert response.status_code == 200
    assert response.json() == local_items[1]
'''
FIXED_TEST = '''from fastapi.testclient import TestClient
from app import app
client = TestClient(app)

def test_get_created_item():
    created = client.post("/items", json={"title": "Created item"})
    assert created.status_code == 201
    item = created.json()
    response = client.get(f"/items/{item['id']}")
    assert response.status_code == 200
    assert response.json() == item
'''


@pytest.mark.parametrize("max_fixes,repair_succeeds", [(1, True), (1, False), (0, True)])
def test_task_check_failure_gets_one_bounded_repair_with_new_failing_file(tmp_path, max_fixes, repair_succeeds):
    class Gateway(FixtureGateway):
        def __init__(self):
            self.calls = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["role"] == "developer" and kwargs["task"] == "build:T2":
                return {"files": {"test_app.py": BROKEN_TEST}}
            if kwargs["role"] == "qa":
                assert "dependency_overrides" in kwargs["inputs"]["files"]["test_app.py"]
                assert "404" in kwargs["inputs"]["qa"]["output"]
                return {"files": {"test_app.py": FIXED_TEST if repair_succeeds else BROKEN_TEST}}
            return super().complete(**kwargs)

    gateway = Gateway()
    runner = Orchestrator(Store(tmp_path), gateway=gateway, sandbox=Sandbox("local"))
    blueprint = load_blueprint("autopilot")
    blueprint.stages = blueprint.stages[:6]
    blueprint.stages[-1].retries = 0
    blueprint.stages[-1].config = {"max_fix_attempts": max_fixes}
    project = runner.create("Preserve this original product", blueprint)
    result = runner.run(project.id)
    succeeds = bool(max_fixes and repair_succeeds)
    assert result.status == ("completed" if succeeds else "failed"), result.error
    assert len([c for c in gateway.calls if c["role"] == "qa"]) == max_fixes
    assert len([c for c in gateway.calls if c["task"] == "build:T1"]) == 1
    assert all(c["inputs"]["idea"] == project.idea for c in gateway.calls)
    checks = [e["data"] for e in runner.store.events(project.id) if e["type"] == "quality.quick_check" and e["data"]["task"] == "T2"]
    assert len(checks) == max_fixes + 1 and checks[0]["failed"] == 1
    assert checks[-1]["green"] == succeeds
    if not succeeds:
        assert "build" not in result.artifacts
        assert "deployment" not in result.artifacts
    else:
        assert any(e["type"] == "task.repairing" for e in runner.store.events(project.id))


def test_agent_start_and_finish_audit_describes_work_and_elapsed_time():
    events = []
    AgentRuntime(FixtureGateway(), lambda kind, **data: events.append((kind, data))).produce(
        "product_manager", "intake", {"idea": "Original product"}, Brief)
    assert events[0][0] == "agent.started"
    assert events[0][1]["input_keys"] == ["idea"] and events[0][1]["output_type"] == "Brief"
    assert events[-1][0] == "agent.call" and events[-1][1]["duration_ms"] >= 0
    assert events[-1][1]["outcome"] == "valid"


def test_replan_feedback_is_used_downstream_without_changing_original_idea(tmp_path):
    class Gateway(FixtureGateway):
        prompts = []

        def complete(self, **kwargs):
            self.prompts.append(json.loads(kwargs["prompt"]))
            return super().complete(**kwargs)

    gateway = Gateway()
    runner = Orchestrator(Store(tmp_path), gateway=gateway, sandbox=Sandbox("local"))
    project = runner.create("Original idea", "planning-only")
    runner.run(project.id)
    runner.rerun(project.id, "intake", "Preserve the requested visible user actions")
    result = runner.run(project.id)
    assert result.idea == "Original idea" and result.status == "awaiting_approval"
    assert all("visible user actions" in prompt["feedback"] for prompt in gateway.prompts[-4:])
    assert all(prompt["inputs"]["idea"] == project.idea for prompt in gateway.prompts)


def test_failed_attempt_files_are_previewable_without_being_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderSettings, "gateway", lambda self: FixtureGateway())
    app = create_app(tmp_path)
    project = app.state.runner.create("Preview failed work", "planning-only")
    workspace = Workspace(app.state.store.directory(project.id) / "workspace")
    (workspace.root / "new_failing_test.py").write_text("assert False\n")
    with TestClient(app) as client:
        files = client.get(f"/api/projects/{project.id}/files").json()
        assert "new_failing_test.py" in files
        assert client.get(f"/api/projects/{project.id}/file", params={"path":"new_failing_test.py"}).text == "assert False\n"
        assert client.get("/static/console.js").status_code == 200
        assert client.get("/static/console.css").status_code == 200


def test_rerun_api_carries_user_feedback_to_the_new_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(ProviderSettings, "gateway", lambda self: FixtureGateway())
    app = create_app(tmp_path)
    project = app.state.runner.create("Original idea", "planning-only")
    app.state.runner.run(project.id)
    with TestClient(app) as client:
        response = client.post(f"/api/projects/{project.id}/rerun", headers={"X-Harness-Request":"1"},
                               json={"stage":"intake", "feedback":"Keep the requested user interface"})
        assert response.status_code == 202
    saved = app.state.store.load(project.id)
    assert saved.status == "awaiting_approval"
    assert saved.idea == "Original idea"
    assert saved.feedback["intake"] == "Keep the requested user interface"


@pytest.mark.parametrize("fixes", [-1, 6, "1"])
def test_invalid_build_repair_budgets_are_rejected(fixes):
    blueprint = load_blueprint("default")
    next(s for s in blueprint.stages if s.name == "build").config = {"max_fix_attempts": fixes}
    with pytest.raises(ValueError, match="max_fix_attempts"):
        validate(blueprint)
