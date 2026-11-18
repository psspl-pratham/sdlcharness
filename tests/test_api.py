import time

import pytest
from fastapi.testclient import TestClient

from fixtures import FixtureGateway
from sdlc_harness.api import create_app
from sdlc_harness.providers import ProviderSettings

HEADERS = {"X-Harness-Request": "1"}


@pytest.fixture(autouse=True)
def offline_gateway(monkeypatch):
    monkeypatch.setattr(ProviderSettings, "gateway", lambda self: FixtureGateway())


def wait_for(client, project_id, status):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        project = client.get(f"/api/projects/{project_id}").json()
        if project["status"] == status:
            return project
        time.sleep(0.02)
    raise AssertionError(project)


def test_console_api_background_gate_and_file_boundaries(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").json()["provider"] == "mock"
        assert client.post("/api/projects", json={"idea": "Tracker"}).status_code == 403
        response = client.post("/api/projects", headers=HEADERS, json={"idea": "Tracker", "blueprint": "planning-only"})
        assert response.status_code == 202
        project_id = response.json()["id"]
        wait_for(client, project_id, "awaiting_approval")
        assert client.get(f"/api/projects/{project_id}/file", params={"path": "../state.json"}).status_code == 400
        assert client.get(f"/api/projects/{project_id}/report").status_code == 200
        assert client.post(f"/api/projects/{project_id}/approve", headers=HEADERS).status_code == 202
        wait_for(client, project_id, "completed")
        assert client.post(f"/api/projects/{project_id}/approve", headers=HEADERS).status_code == 400
        assert client.get("/api/projects/000000000000").status_code == 404
        assert len(client.get("/api/projects").json()) == 1


def test_rejects_unknown_blueprint_and_empty_idea(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/projects", headers=HEADERS, json={"idea": "Tracker", "blueprint": "/etc/passwd"}).status_code == 400
        assert client.post("/api/projects", headers=HEADERS, json={"idea": ""}).status_code == 422
