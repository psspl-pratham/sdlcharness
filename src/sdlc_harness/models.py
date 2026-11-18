from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Role = Literal["backend", "frontend", "qa", "devops"]


class Member(Model):
    name: str = Field(min_length=1)
    role: Role
    points_per_sprint: int = Field(gt=0)


class Team(Model):
    sprint_length_days: int = Field(default=14, gt=0)
    focus_factor: float = Field(default=0.8, gt=0, le=1)
    allow_cross_role: bool = True
    members: list[Member] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_members(self):
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError("Team member names must be unique")
        if any(int(m.points_per_sprint * self.focus_factor) < 1 for m in self.members):
            raise ValueError("Every member must have at least one point of effective capacity")
        return self


class StageConfig(Model):
    name: str
    enabled: bool = True
    gate: Literal["automatic", "manual"] = "automatic"
    retries: int = Field(default=1, ge=0, le=5)
    config: dict[str, Any] = Field(default_factory=dict)


class Blueprint(Model):
    name: str
    team: Team
    stages: list[StageConfig] = Field(min_length=1)


class Brief(Model):
    name: str
    problem: str
    users: list[str]
    entities: list[str]
    constraints: list[str]


class Story(Model):
    id: str
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: list[str] = Field(min_length=1)
    priority: Literal["must", "should", "could", "wont"] = "must"


class Requirements(Model):
    stories: list[Story] = Field(min_length=1)
    nfrs: list[str]


class Endpoint(Model):
    method: str
    path: str
    description: str


class Architecture(Model):
    style: str
    stack: list[str]
    components: list[str]
    data_model: dict[str, Any]
    api: list[Endpoint]
    decisions: list[str]


class Epic(Model):
    id: str
    title: str
    priority: int = Field(default=0, ge=0)


class Task(Model):
    id: str
    epic: str
    title: str
    story_ids: list[str]
    role: Role
    points: int = Field(gt=0)
    priority: int = Field(default=0, ge=0)
    dependencies: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    status: Literal["todo", "in_progress", "done"] = "todo"
    assignee: str | None = None
    sprint: int | None = None


class Backlog(Model):
    epics: list[Epic] = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_ids(self):
        ids = [t.id for t in self.tasks]
        epics = [e.id for e in self.epics]
        if len(set(ids)) != len(ids) or len(set(epics)) != len(epics):
            raise ValueError("Task and epic IDs must be unique")
        if any(t.epic not in epics for t in self.tasks):
            raise ValueError("Every task must reference a known epic")
        return self


class Sprint(Model):
    number: int
    start: date
    end: date
    goal: str
    tasks: list[str]
    capacity: int
    committed: int
    load: dict[str, int]


class SprintPlan(Model):
    sprints: list[Sprint]
    critical_path: list[str]
    critical_path_points: int
    tails: dict[str, int]
    projected_end: date
    warnings: list[str]
    repairs: list[str]


class FilePatch(Model):
    files: dict[str, str] = Field(min_length=1)
    notes: str = ""


class Build(Model):
    files: list[str]
    commits: list[dict[str, Any]]
    commit: str


class QA(Model):
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    compile_errors: list[str] = Field(default_factory=list)
    dependency_errors: list[str] = Field(default_factory=list)
    failures: list[dict[str, str]] = Field(default_factory=list)
    attempts: int = 1
    green: bool = False
    output: str = ""


class Deployment(Model):
    target: Literal["local", "docker"]
    status: Literal["healthy", "unhealthy", "stopped"]
    url: str
    image: str | None = None
    pid: int | None = None
    process_start: str | None = None
    container: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    logs: str = ""


class Retro(Model):
    metrics: dict[str, Any]
    went_well: list[str]
    to_improve: list[str]
    actions: list[str]


ARTIFACT_MODELS = {
    "brief": Brief, "requirements": Requirements, "architecture": Architecture,
    "backlog": Backlog, "sprint_plan": SprintPlan, "build": Build,
    "qa": QA, "deployment": Deployment, "retro": Retro,
}


class BuildProgress(Model):
    head: str
    commits: list[dict[str, Any]] = Field(default_factory=list)
    completed_sprints: list[int] = Field(default_factory=list)
    sprint: int | None = None
    task: str | None = None
    phase: Literal["generate", "check"] = "generate"
    changed_files: list[str] = Field(default_factory=list)
    repair_attempts: int = 0
    repair_history: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""


class StageRun(Model):
    status: Literal["pending", "running", "awaiting_approval", "done", "failed", "skipped"] = "pending"
    attempts: int = 0
    started: str | None = None
    finished: str | None = None
    error: str | None = None
    workspace_base: str | None = None
    build_progress: BuildProgress | None = None


class ProviderInfo(Model):
    # Keep the legacy value only to read historical ledgers; runtime is Bedrock-only.
    name: Literal["mock", "bedrock"] = "mock"
    model_id: str | None = None
    region: str | None = None


class Project(Model):
    id: str
    idea: str = Field(min_length=1, max_length=8000)
    blueprint: Blueprint
    start_date: date
    provider: ProviderInfo = Field(default_factory=ProviderInfo)
    max_model_calls: int = Field(default=40, ge=1)
    model_calls: int = Field(default=0, ge=0)
    created: str = Field(default_factory=now)
    status: Literal["created", "running", "awaiting_approval", "completed", "failed"] = "created"
    feedback: dict[str, str] = Field(default_factory=dict)
    stages: dict[str, StageRun]
    artifacts: dict[str, Any] = Field(default_factory=dict)
    stage_artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: str | None = None

    @model_validator(mode="after")
    def validate_artifacts(self):
        for key, value in self.artifacts.items():
            if key not in ARTIFACT_MODELS:
                raise ValueError(f"Unknown artifact: {key}")
            ARTIFACT_MODELS[key].model_validate(value)
        return self
