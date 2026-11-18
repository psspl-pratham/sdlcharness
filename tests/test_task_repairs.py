"""Real pytest failures exercise repair history, state isolation, and saved task progress."""
import pytest
from fastapi.testclient import TestClient

from fixtures import FixtureGateway
from sdlc_harness.api import create_app
from sdlc_harness.blueprints import load_blueprint
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.providers import ProviderSettings
from sdlc_harness.quality import QualityEngine
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store
from sdlc_harness.workspace import Workspace


BROKEN_APP = '''from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
app = FastAPI()
notes = {}
pinned_notes = []
class Note(BaseModel):
    content: str
    is_pinned: bool = False
@app.get('/health')
def health(): return {'status': 'ok'}
@app.post('/notes')
def create_note(note: Note):
    if note.is_pinned and len(pinned_notes) >= 3:
        raise HTTPException(400, 'Maximum 3 pinned notes allowed')
    note_id = str(len(notes) + 1)
    notes[note_id] = note
    if note.is_pinned: pinned_notes.append({'note_id': note_id})
    return {'note_id': note_id}
@app.post('/notes/{note_id}/pin')
def pin_note(note_id: str):
    if len(pinned_notes) >= 3: raise HTTPException(400, 'Maximum 3 pinned notes allowed')
    pinned_notes.append({'note_id': note_id})
    return {'message': 'Note pinned'}
@app.delete('/notes/{note_id}/pin')
def unpin_note(note_id: str):
    if note_id not in pinned_notes: raise HTTPException(400, 'Note not pinned')
    pinned_notes = [p for p in pinned_notes if p['note_id'] != note_id]
    return {'message': 'Note unpinned'}
'''
FIXED_APP = BROKEN_APP.replace(
    "if note_id not in pinned_notes:", "if not any(p['note_id'] == note_id for p in pinned_notes):",
).replace("pinned_notes = [p for", "pinned_notes[:] = [p for")
PIN_TESTS = '''from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
def test_pin_note():
    note_id = client.post('/notes', json={'content': 'first'}).json()['note_id']
    assert client.post(f'/notes/{note_id}/pin').status_code == 200
def test_unpin_note():
    note_id = client.post('/notes', json={'content': 'second'}).json()['note_id']
    assert client.post(f'/notes/{note_id}/pin').status_code == 200
    assert client.delete(f'/notes/{note_id}/pin').status_code == 200
def test_max_pinned_notes():
    for i in range(3):
        assert client.post('/notes', json={'content': str(i), 'is_pinned': True}).status_code == 200
    assert client.post('/notes', json={'content': 'fourth', 'is_pinned': True}).status_code == 400
'''
CLEAN_STATE = '''import pytest
import app
@pytest.fixture(autouse=True)
def clean_state():
    app.notes.clear()
    app.pinned_notes.clear()
    yield
    app.notes.clear()
    app.pinned_notes.clear()
'''


class RepairGateway(FixtureGateway):
    def __init__(self, fixes=False):
        self.fixes, self.calls, self.repairs = fixes, [], 0

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['task'] == 'build:T2':
            return {'files': {'app.py': BROKEN_APP, 'test_pin_note.py': PIN_TESTS}}
        if kwargs['role'] == 'qa':
            self.repairs += 1
            if self.fixes:
                return {'files': {'app.py': FIXED_APP, 'conftest.py': CLEAN_STATE}}
            return {'files': {'app.py': BROKEN_APP}, 'notes': 'Claimed fixed; tests must verify this'}
        return super().complete(**kwargs)


def runner_for(tmp_path, gateway, max_fixes=1):
    runner = Orchestrator(Store(tmp_path), gateway, sandbox=Sandbox())
    bp = load_blueprint('autopilot')
    bp.stages = bp.stages[:6]
    bp.stages[-1].config['max_fix_attempts'] = max_fixes
    return runner, runner.create('Notes with up to three pins', bp)


def test_real_failed_tests_are_structured_in_repair_context(tmp_path):
    runner, project = runner_for(tmp_path, RepairGateway(), max_fixes=0)
    result = runner.run(project.id)
    failures = [e['data'] for e in runner.store.events(project.id, 1000) if e['type'] == 'quality.quick_check'][-1]['failures']
    assert len(failures) == 2
    scope_error = next(f for f in failures if f['test'].endswith('::test_unpin_note'))
    assert 'UnboundLocalError' in scope_error['message']
    assert 'pinned_notes' in scope_error['traceback']
    assert any(f['test'].endswith('::test_max_pinned_notes') for f in failures)


def test_three_repairs_use_results_from_each_attempt_without_rebuilding_passed_tasks(tmp_path):
    class IteratingGateway(RepairGateway):
        def complete(self, **kwargs):
            if kwargs['role'] != 'qa':
                return super().complete(**kwargs)
            self.calls.append(kwargs)
            self.repairs += 1
            history = kwargs['inputs']['repair_history']
            assert history[-1]['result']['green'] is False
            assert kwargs['inputs']['qa']['failures']
            if self.repairs == 1:
                return {'files': {'app.py': BROKEN_APP}, 'notes': 'Claimed fixed'}
            if self.repairs == 2:
                assert len(history) == 2 and history[-1]['previous_patch_notes'] == 'Claimed fixed'
                return {'files': {'app.py': FIXED_APP}}
            assert kwargs['inputs']['qa']['failed'] == 1
            return {'files': {'conftest.py': CLEAN_STATE}}

    gateway = IteratingGateway()
    runner, project = runner_for(tmp_path, gateway, max_fixes=3)
    result = runner.run(project.id)
    assert result.status == 'completed', result.error
    assert gateway.repairs == 3
    assert len([c for c in gateway.calls if c['task'] == 'build:T1']) == 1
    checks = [e['data'] for e in runner.store.events(project.id, 1000)
              if e['type'] == 'quality.quick_check' and e['data']['task'] == 'T2']
    assert [c['failed'] for c in checks] == [2, 2, 1, 0]
    assert checks[-1]['passed'] == 4
    attempts = [e['data'] for e in runner.store.events(project.id, 1000) if e['type'] == 'task.repairing']
    assert attempts[1]['unchanged'] is True


@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_repair_preserves_commits_and_uses_current_failed_files(tmp_path, legacy):
    gateway = RepairGateway()
    runner, project = runner_for(tmp_path, gateway)
    failed = runner.run(project.id)
    assert failed.status == 'failed' and failed.stages['build'].attempts == 1
    progress = failed.stages['build'].build_progress
    first_commit = progress.commits[0]['commit']
    calls_before = failed.model_calls
    if legacy:
        failed.stages['build'].build_progress = None
        runner.store.save(failed)
    restarted_gateway = RepairGateway(fixes=True)
    restarted = Orchestrator(runner.store, restarted_gateway, sandbox=Sandbox())
    restarted.repair_task(project.id)
    result = restarted.run(project.id)
    assert result.status == 'completed', result.error
    assert result.model_calls == calls_before + 2  # one repair, then the remaining packaging task
    assert result.artifacts['build']['commits'][0]['commit'] == first_commit
    assert not any(c['task'] in {'build:T1', 'build:T2'} for c in restarted_gateway.calls)
    workspace = Workspace(runner.store.directory(project.id) / 'workspace')
    assert workspace.git('log', '--format=%s').count('T1:') == 1


def test_exhausted_repair_stays_stopped_on_run_and_cannot_replenish_call_limit(tmp_path):
    gateway = RepairGateway()
    runner, project = runner_for(tmp_path, gateway)
    failed = runner.run(project.id)
    before = len(gateway.calls)
    runner.run(project.id)
    assert len(gateway.calls) == before
    failed.max_model_calls = failed.model_calls
    runner.store.save(failed)
    with pytest.raises(ValueError, match='model-call limit'):
        runner.repair_task(project.id)
    assert len(gateway.calls) == before


def test_changed_commit_checkpoint_rejects_resume_before_calling_the_model(tmp_path):
    gateway = RepairGateway()
    runner, project = runner_for(tmp_path, gateway, max_fixes=0)
    runner.run(project.id)
    workspace = Workspace(runner.store.directory(project.id) / 'workspace')
    workspace.commit('Unexpected out-of-band commit')
    before = len(gateway.calls)
    with pytest.raises(ValueError, match='HEAD differs'):
        runner.repair_task(project.id)
    assert len(gateway.calls) == before


def test_repair_api_requires_write_header_and_resumes_saved_task(tmp_path, monkeypatch):
    gateway = RepairGateway()
    monkeypatch.setattr(ProviderSettings, 'gateway', lambda _: gateway)
    app = create_app(tmp_path)
    app.state.runner.sandbox = Sandbox()
    app.state.runner.quality = QualityEngine(Sandbox())
    bp = load_blueprint('autopilot')
    bp.stages = bp.stages[:6]
    bp.stages[-1].config['max_fix_attempts'] = 0
    project = app.state.runner.create('Repair API example', bp)
    app.state.runner.run(project.id)
    # Give the explicitly requested repair a finite attempt budget.
    saved = app.state.store.load(project.id)
    saved.blueprint.stages[-1].config['max_fix_attempts'] = 1
    app.state.store.save(saved)
    gateway.fixes = True
    with TestClient(app) as client:
        url = f'/api/projects/{project.id}/repair-task'
        assert client.post(url, json={}).status_code == 403
        assert client.post(url, json={}, headers={'X-Harness-Request': '1'}).status_code == 202
    assert app.state.store.load(project.id).status == 'completed'
