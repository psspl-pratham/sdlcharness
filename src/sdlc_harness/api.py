import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .blueprints import names
from .models import Model
from .providers import ProviderSettings, load_environment
from .store import BusyError, Store
from .workspace import Workspace, safe_path


class CreateRequest(Model):
    idea: str = Field(min_length=1, max_length=8000)
    blueprint: str = "default"
    start_date: date | None = None


class FeedbackRequest(Model):
    feedback: str = Field(min_length=1, max_length=8000)


class RerunRequest(Model):
    stage: str
    feedback: str = Field(default="", max_length=8000)


class RepairRequest(Model):
    feedback: str = Field(default="", max_length=8000)


def create_app(data_dir=None, sandbox_mode=None, settings: ProviderSettings | None = None):
    load_environment()
    store = Store(data_dir or os.environ.get("HARNESS_DATA_DIR", "data/projects"))
    settings = settings or ProviderSettings.from_env(sandbox=sandbox_mode)
    runner = settings.runner(store)
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="harness")

    def submit(project_id):
        def work():
            try:
                runner.run(project_id)
            except BusyError:
                pass  # Another process owns this project.
            except Exception as error:
                store.event(project_id, "worker.failed", message=str(error))
                with store.lock(project_id):
                    project = store.load(project_id)
                    project.status, project.error = "failed", str(error)
                    store.save(project)
        pool.submit(work)

    @asynccontextmanager
    async def lifespan(app):
        for project in store.projects():
            if project.status in {"created", "running"}:
                try:
                    runner.gateway_for(project)
                except ValueError:
                    continue  # Keep projects for another configured model available for inspection.
                submit(project.id)
        yield
        pool.shutdown(wait=True)

    app = FastAPI(title="SDLC Harness POC", version="0.1.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.state.store, app.state.runner = store, runner

    @app.middleware("http")
    async def local_write_guard(request: Request, call_next):
        # A custom header prevents drive-by cross-origin writes to this local, unauthenticated POC.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.headers.get("x-harness-request") != "1":
            return JSONResponse({"detail": "Set X-Harness-Request: 1 for mutations"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(BusyError)
    async def busy_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_handler(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_handler(request, error):
        return JSONResponse({"detail": "Project or file not found"}, status_code=404)

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok", "provider": runner.gateway.name, "model_id": getattr(runner.gateway, "model_id", None),
                "region": getattr(runner.gateway, "region", None), "sandbox": runner.sandbox.mode,
                "max_output_tokens": settings.max_output_tokens, "max_model_calls": settings.max_model_calls}

    @app.get("/api/blueprints")
    def blueprints():
        return names()

    @app.get("/api/projects")
    def projects():
        return [{"id": p.id, "idea": p.idea, "status": p.status, "created": p.created,
                 "blueprint": p.blueprint.name, "provider": p.provider} for p in store.projects()]

    @app.post("/api/projects", status_code=202)
    def create(body: CreateRequest):
        if body.blueprint not in names():
            raise ValueError("Select a registered blueprint")
        project = runner.create(body.idea, body.blueprint, body.start_date)
        submit(project.id)
        return project

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        return store.load(project_id)

    @app.get("/api/projects/{project_id}/events")
    def events(project_id: str, limit: int = Query(default=200, ge=1, le=1000)):
        store.load(project_id)
        return store.events(project_id, limit)

    @app.post("/api/projects/{project_id}/run", status_code=202)
    def run(project_id: str):
        runner.gateway_for(store.load(project_id))
        submit(project_id)
        return {"id": project_id, "queued": True}

    @app.post("/api/projects/{project_id}/approve", status_code=202)
    def approve(project_id: str):
        runner.gateway_for(store.load(project_id))
        project = runner.approve(project_id)
        submit(project_id)
        return project

    @app.post("/api/projects/{project_id}/send-back", status_code=202)
    def send_back(project_id: str, body: FeedbackRequest):
        runner.gateway_for(store.load(project_id))
        project = runner.send_back(project_id, body.feedback)
        submit(project_id)
        return project

    @app.post("/api/projects/{project_id}/rerun", status_code=202)
    def rerun(project_id: str, body: RerunRequest):
        runner.gateway_for(store.load(project_id))
        project = runner.rerun(project_id, body.stage, body.feedback)
        submit(project_id)
        return project

    @app.post("/api/projects/{project_id}/stop")
    def stop(project_id: str):
        return runner.stop_deployment(project_id)

    @app.post("/api/projects/{project_id}/repair-task", status_code=202)
    def repair_task(project_id: str, body: RepairRequest):
        project = runner.repair_task(project_id, body.feedback)
        submit(project_id)
        return project

    @app.get("/api/projects/{project_id}/files")
    def files(project_id: str):
        store.load(project_id)
        return Workspace(store.directory(project_id) / "workspace").files(include_untracked=True)

    @app.get("/api/projects/{project_id}/file", response_class=PlainTextResponse)
    def file(project_id: str, path: str):
        store.load(project_id)
        target = safe_path(store.directory(project_id) / "workspace", path)
        if target.stat().st_size > 1_000_000:
            raise HTTPException(status_code=413, detail="File exceeds preview limit")
        return target.read_text(errors="replace")

    @app.get("/api/projects/{project_id}/report", response_class=PlainTextResponse)
    def report(project_id: str):
        store.load(project_id)
        return (store.directory(project_id) / "REPORT.md").read_text()

    return app
