import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import Blueprint, Project, ProviderInfo, StageRun, now


class BusyError(ValueError):
    pass


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", project_id):
            raise ValueError("Invalid project ID")
        return self.root / project_id

    def create(self, idea: str, blueprint: Blueprint, start_date, *, provider: ProviderInfo,
               max_model_calls: int = 40) -> Project:
        project = Project(
            id=uuid4().hex[:12], idea=idea.strip(), blueprint=blueprint,
            start_date=start_date, stages={s.name: StageRun() for s in blueprint.stages},
            provider=provider, max_model_calls=max_model_calls,
        )
        self.directory(project.id).mkdir()
        self.save(project)
        self.event(project.id, "project.created", message=project.idea)
        return project

    def load(self, project_id: str) -> Project:
        return Project.model_validate_json((self.directory(project_id) / "state.json").read_text())

    def save(self, project: Project):
        directory = self.directory(project.id)
        fd, path = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(project.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, directory / "state.json")
            descriptor = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            Path(path).unlink(missing_ok=True)

    @contextmanager
    def lock(self, project_id: str):
        directory = self.directory(project_id)
        if not directory.is_dir():
            raise FileNotFoundError(project_id)
        with (directory / "worker.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BusyError("This project already has an active worker") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def event(self, project_id, event_type, stage=None, message="", **data):
        event = {"timestamp": now(), "type": event_type, "stage": stage, "message": message, "data": data}
        with (self.directory(project_id) / "events.jsonl").open("a+b") as stream:
            # Discard only an uncommitted partial tail left by a killed writer.
            stream.seek(0)
            existing = stream.read()
            if existing and not existing.endswith(b"\n"):
                stream.truncate(existing.rfind(b"\n") + 1)
            stream.write((json.dumps(event) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())

    def events(self, project_id: str, limit: int = 200) -> list[dict]:
        path = self.directory(project_id) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines(keepends=True)
        events = []
        for line in lines:
            # A killed writer may leave an incomplete final record.
            if line.endswith("\n"):
                events.append(json.loads(line))
        return events[-limit:]

    def projects(self) -> list[Project]:
        return sorted(
            [self.load(p.parent.name) for p in self.root.glob("*/state.json")],
            key=lambda p: p.created, reverse=True,
        )
