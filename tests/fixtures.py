"""Deterministic test data; never imported by the application."""
import re

APP_SOURCE = '''from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Harness sample service")
items: dict[int, dict] = {}
next_id = 1


class ItemInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items")
def list_items():
    return list(items.values())


@app.post("/items", status_code=201)
def create_item(body: ItemInput):
    global next_id
    item = {"id": next_id, "title": body.title}
    items[next_id] = item
    next_id += 1
    return item


@app.get("/items/{item_id}")
def get_item(item_id: int):
    if item_id not in items:
        raise HTTPException(status_code=404, detail="Item not found")
    return items[item_id]
'''

TEST_SOURCE = '''import pytest
from fastapi.testclient import TestClient
import app as module


@pytest.fixture
def client():
    module.items.clear()
    module.next_id = 1
    with TestClient(module.app) as client:
        yield client


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_list_and_fetch(client):
    response = client.post("/items", json={"title": "First item"})
    assert response.status_code == 201
    item = response.json()
    assert client.get("/items").json() == [item]
    assert client.get(f"/items/{item['id']}").json() == item


def test_invalid_title(client):
    assert client.post("/items", json={"title": ""}).status_code == 422


def test_missing_item(client):
    assert client.get("/items/999").status_code == 404


def test_openapi(client):
    assert "/items" in client.get("/openapi.json").json()["paths"]
'''


class FixtureGateway:
    """A deterministic fixture, not a model that implements arbitrary product ideas."""
    name = "mock"

    def complete(self, *, role, task, inputs, prompt, max_tokens, timeout):
        if task == "intake":
            words = re.findall(r"[\w]+", inputs["idea"])
            return {
                "name": " ".join(words[:6]) or "Sample service", "problem": inputs["idea"],
                "users": ["Demo user"], "entities": ["Item"],
                "constraints": ["Offline fixture: always generates the sample item service", "In-memory data resets on restart"],
            }
        if task == "requirements":
            return {"stories": [{
                "id": "S1", "as_a": "demo user", "i_want": "create and retrieve items",
                "so_that": "I can track a small list", "priority": "must",
                "acceptance_criteria": ["POST /items returns 201", "GET /items lists created items", "Missing items return 404", "Empty titles return 422"],
            }], "nfrs": ["GET /health returns 200", "OpenAPI describes the service", "Single process; in-memory storage"]}
        if task == "architecture":
            return {
                "style": "single-service", "stack": ["Python 3.12", "FastAPI", "pytest"],
                "components": ["HTTP API", "in-memory item store"],
                "data_model": {"Item": {"id": "integer", "title": "nonempty string"}},
                "api": [{"method": m, "path": p, "description": d} for m, p, d in [
                    ("GET", "/health", "Liveness"), ("GET", "/items", "List items"),
                    ("POST", "/items", "Create item"), ("GET", "/items/{item_id}", "Retrieve item"),
                ]], "decisions": ["Keep the offline sample deliberately small and reproducible"],
            }
        if task == "breakdown":
            return {"epics": [{"id": "E1", "title": "Item service", "priority": 0}, {"id": "E2", "title": "Delivery", "priority": 1}], "tasks": [
                {"id": "T1", "epic": "E1", "title": "Implement item API", "story_ids": ["S1"], "role": "backend", "points": 3, "files": ["app.py", "test_health.py"]},
                {"id": "T2", "epic": "E1", "title": "Verify acceptance criteria", "story_ids": ["S1"], "role": "qa", "points": 2, "dependencies": ["T1"], "files": ["test_app.py"]},
                {"id": "T3", "epic": "E2", "title": "Package and document service", "story_ids": ["S1"], "role": "devops", "points": 2, "dependencies": ["T2"], "files": ["Dockerfile", "requirements.txt", "README.md", ".github/workflows/ci.yml"]},
            ]}
        if role == "developer":
            task_id = inputs["task"]["id"]
            if task_id == "T1":
                return {"files": {"app.py": APP_SOURCE, "test_health.py": 'from fastapi.testclient import TestClient\nfrom app import app\n\ndef test_liveness():\n    assert TestClient(app).get("/health").status_code == 200\n'}}
            if task_id == "T2":
                return {"files": {"test_app.py": TEST_SOURCE}}
            return {"files": {
                "requirements.txt": "fastapi>=0.115,<1\nuvicorn>=0.30,<1\npytest>=8,<10\nhttpx>=0.27,<1\n",
                "Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE 8000\nCMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]\n',
                "README.md": "# Offline sample service\n\nGenerated by the SDLC Harness mock provider. Stores items in memory.\n\nInstall: `pip install -r requirements.txt`\n\nRun: `python -m uvicorn app:app`\n\nTest: `python -m pytest`\n\nGET /health, GET /items, POST /items, GET /items/{item_id}.\n",
                ".github/workflows/ci.yml": 'name: tests\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n      - run: pip install -r requirements.txt\n      - run: python -m pytest --junitxml=results.xml\n',
            }}
        if role == "qa":
            return {"files": {"app.py": APP_SOURCE, "test_app.py": TEST_SOURCE}, "notes": "Restore the known offline fixture after an observed failure"}
        if task == "retro":
            return {"metrics": inputs["metrics"], "went_well": ["The sample passed executable checks and health checks"],
                    "to_improve": ["The offline provider implements a fixed sample only"],
                    "actions": ["Connect a live model gateway after verifying this workflow"]}
        raise ValueError(f"Unsupported mock task: {role}/{task}")
