import json
import sys

import pytest

from sdlc_harness.agents import AgentRuntime
from fixtures import FixtureGateway
from sdlc_harness.models import Brief, FilePatch
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import BusyError, Store
from sdlc_harness.workspace import Workspace


def test_agent_validation_retry_has_schema_feedback_and_audit():
    class Gateway:
        name = "test"
        calls = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"broken": True}
            return FixtureGateway().complete(**kwargs)

    gateway, events = Gateway(), []
    result = AgentRuntime(gateway, lambda kind, **data: events.append(data) if kind == "agent.call" else None).produce(
        "product_manager", "intake", {"idea": "Sample"}, Brief, "Keep it small",
    )
    assert result.problem == "Sample"
    assert "Validation error" in gateway.calls[1]["prompt"]
    assert "Keep it small" in gateway.calls[0]["prompt"]
    assert [e["outcome"] for e in events] == ["invalid", "valid"]


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", ".git/config", "foo/../../escape", "foo\\bar.py"])
def test_patch_rejects_unsafe_paths_before_writing(tmp_path, path):
    workspace = Workspace(tmp_path / "workspace")
    with pytest.raises(ValueError):
        workspace.apply(FilePatch(files={"safe.py": "ok", path: "bad"}), "developer")
    assert not (workspace.root / "safe.py").exists()


def test_patch_rejects_symlink_and_wrong_role(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace.root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace.apply(FilePatch(files={"link/code.py": "bad"}), "developer")
    with pytest.raises(ValueError):
        workspace.apply(FilePatch(files={"safe.py": "bad"}), "architect")


def test_project_lock_excludes_other_instances(tmp_path):
    store = Store(tmp_path)
    project_id = "012345abcdef"
    store.directory(project_id).mkdir()
    with store.lock(project_id):
        with pytest.raises(BusyError):
            with Store(tmp_path).lock(project_id):
                pass


def test_event_log_recovers_a_partial_final_write(tmp_path):
    store = Store(tmp_path)
    project_id = "012345abcdef"
    store.directory(project_id).mkdir()
    store.event(project_id, "first")
    with (store.directory(project_id) / "events.jsonl").open("ab") as stream:
        stream.write(b'{"type":"interrupted')
    assert [e["type"] for e in store.events(project_id)] == ["first"]
    store.event(project_id, "recovered")
    assert [e["type"] for e in store.events(project_id)] == ["first", "recovered"]


def test_sandbox_timeout_and_environment_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "do-not-leak")
    result = Sandbox(timeout=0.2).run(tmp_path, ["-c", "import time; time.sleep(10)"])
    assert result.returncode == 124
    result = Sandbox().run(tmp_path, ["-c", "import os; print(os.environ.get('MODEL_API_KEY', 'absent'))"])
    assert result.returncode == 0
    assert result.output.strip() == "absent"
