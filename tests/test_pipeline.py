import json
from datetime import date
from urllib.request import ProxyHandler, build_opener

import pytest

from fixtures import FixtureGateway
from sdlc_harness.blueprints import load_blueprint
from sdlc_harness.deploy import Deployer, process_start
from sdlc_harness.models import Deployment, QA
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store
from sdlc_harness.workspace import Workspace


def make_runner(tmp_path):
    return Orchestrator(Store(tmp_path / "projects"), gateway=FixtureGateway(), sandbox=Sandbox("local"))


def test_manual_gates_send_back_and_resume_across_runner_instances(tmp_path):
    runner = make_runner(tmp_path)
    project = runner.create("A small tracker", "planning-only", date(2026, 11, 18))
    project = runner.run(project.id)
    assert project.status == "awaiting_approval"
    expected = project.artifacts["sprint_plan"]
    assert runner.run(project.id).stages["intake"].attempts == 1
    runner.send_back(project.id, "Review the assigned roles")
    assert "sprint_plan" not in runner.store.load(project.id).artifacts
    project = runner.run(project.id)
    assert project.artifacts["sprint_plan"] == expected
    assert project.feedback["sprint_plan"] == "Review the assigned roles"
    restarted = make_runner(tmp_path)
    restarted.approve(project.id)
    project = restarted.run(project.id)
    assert project.status == "completed"
    assert len([e for e in runner.store.events(project.id) if e["type"] == "agent.call"]) == 4
    assert (runner.store.directory(project.id) / "REPORT.md").exists()


def test_interrupted_stage_restarts_without_repeating_completed_stages(tmp_path):
    runner = make_runner(tmp_path)
    project = runner.create("Tracker", "planning-only")
    project = runner.run(project.id)
    runner.rerun(project.id, "architecture")
    project = runner.store.load(project.id)
    project.status = "running"
    project.stages["architecture"].status = "running"
    project.stages["architecture"].attempts = 1
    runner.store.save(project)
    result = make_runner(tmp_path).run(project.id)
    assert result.status == "awaiting_approval"
    assert result.stages["architecture"].attempts == 1
    assert result.stages["intake"].attempts == 1
    assert any(e["type"] == "stage.recovered" for e in runner.store.events(project.id))


def test_exhausted_stage_does_not_continue_or_retry_forever(tmp_path):
    class BrokenMock(FixtureGateway):
        def complete(self, **kwargs):
            raise RuntimeError("Provider unavailable")

    runner = Orchestrator(Store(tmp_path), gateway=BrokenMock(), sandbox=Sandbox("local"))
    project = runner.create("Tracker", "autopilot")
    result = runner.run(project.id)
    assert result.status == "failed"
    assert result.stages["intake"].attempts == 2
    assert result.stages["requirements"].status == "pending"
    assert runner.run(project.id).stages["intake"].attempts == 2
    runner.rerun(project.id, "intake")
    assert runner.store.load(project.id).stages["intake"].attempts == 0


def test_red_qa_cannot_reach_deploy(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    blueprint = load_blueprint("autopilot")
    for stage in blueprint.stages:
        stage.retries = 0
        if stage.name == "qa":
            stage.config["max_fix_attempts"] = 0
    project = runner.create("Tracker", blueprint)
    original = runner._execute

    def execute(project, stage, workspace):
        if stage.name == "qa":
            monkeypatch.setattr(runner.quality, "check", lambda workspace: QA(failed=1, output="Observed failure"))
        return original(project, stage, workspace)

    monkeypatch.setattr(runner, "_execute", execute)
    result = runner.run(project.id)
    assert result.status == "failed", result.error
    assert result.stages["qa"].status == "failed"
    assert result.artifacts["qa"]["failed"] == 1
    assert result.stages["deploy"].status == "pending"
    assert "deployment" not in result.artifacts


def test_autopilot_real_tests_git_deployment_and_rerun(tmp_path):
    runner = make_runner(tmp_path)
    project = runner.create("A small tracker", "autopilot")
    try:
        result = runner.run(project.id)
        assert result.status == "completed", result.error
        assert result.artifacts["qa"]["green"]
        assert result.artifacts["qa"]["passed"] == 6
        assert result.artifacts["deployment"]["status"] == "healthy"
        opener = build_opener(ProxyHandler({}))
        with opener.open(result.artifacts["deployment"]["url"] + "/health") as response:
            assert response.status == 200
        workspace = Workspace(runner.store.directory(project.id) / "workspace")
        history = workspace.git("log", "--format=%s")
        assert "T1:" in history and "T2:" in history and "T3:" in history
        assert "Merge sprint 1" in history
        before = len(runner.store.events(project.id))
        assert runner.run(project.id).status == "completed"
        assert len(runner.store.events(project.id)) == before
        runner.rerun(project.id, "build")
        reset = runner.store.load(project.id)
        assert "deployment" not in reset.artifacts and "qa" not in reset.artifacts
        assert all(t["status"] == "todo" for t in reset.artifacts["backlog"]["tasks"])
        assert not (workspace.root / "app.py").exists()
        result = runner.run(project.id)
        assert result.status == "completed", result.error
        assert runner.stop_deployment(project.id).artifacts["deployment"]["status"] == "stopped"
    finally:
        current = runner.store.load(project.id)
        if "deployment" in current.artifacts:
            runner.deployer.stop(Deployment.model_validate(current.artifacts["deployment"]))


def test_interrupted_build_restores_checkpoint_and_replays_tasks(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    blueprint = load_blueprint("autopilot")
    blueprint.stages = blueprint.stages[:6]
    project = runner.create("Tracker", blueprint)
    check = runner.quality.check
    calls = 0

    def interrupted(workspace):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("Simulated worker termination")
        return check(workspace)

    monkeypatch.setattr(runner.quality, "check", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner.run(project.id)
    assert runner.store.load(project.id).stages["build"].status == "running"
    restarted = make_runner(tmp_path)
    result = restarted.run(project.id)
    assert result.status == "completed", result.error
    workspace = Workspace(runner.store.directory(project.id) / "workspace")
    assert workspace.git("log", "--format=%s").count("T1:") == 1
    assert all(task["status"] == "done" for task in result.artifacts["backlog"]["tasks"])


def test_qa_repairs_observed_compile_failure_and_rechecks(tmp_path):
    runner = make_runner(tmp_path)
    project = runner.create("Tracker")
    runner.run(project.id)
    runner.approve(project.id)
    result = runner.run(project.id)
    assert result.stages["qa"].status == "awaiting_approval", result.error
    runner.send_back(project.id, "Repair the observed syntax failure")
    workspace = Workspace(runner.store.directory(project.id) / "workspace")
    (workspace.root / "app.py").write_text("def invalid(:\n")
    result = runner.run(project.id)
    assert result.status == "awaiting_approval", result.error
    assert result.artifacts["qa"]["green"]
    assert result.artifacts["qa"]["attempts"] == 2
    assert any(e["type"] == "agent.call" and e["data"]["role"] == "qa" for e in runner.store.events(project.id))


def test_interrupted_deployment_is_cleaned_up_on_resume(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    project = runner.create("Tracker", "autopilot")
    deploy = runner.deployer.deploy

    def interrupted(*args, **kwargs):
        checkpoint = kwargs["checkpoint"]

        def crash(record):
            checkpoint(record)
            raise KeyboardInterrupt("Simulated worker termination after launching service")

        kwargs["checkpoint"] = crash
        return deploy(*args, **kwargs)

    monkeypatch.setattr(runner.deployer, "deploy", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run(project.id)
        old_pid = runner.store.load(project.id).artifacts["deployment"]["pid"]
        restarted = make_runner(tmp_path)
        result = restarted.run(project.id)
        assert result.status == "completed", result.error
        assert process_start(old_pid) is None
    finally:
        current = runner.store.load(project.id)
        if "deployment" in current.artifacts:
            runner.deployer.stop(Deployment.model_validate(current.artifacts["deployment"]))
