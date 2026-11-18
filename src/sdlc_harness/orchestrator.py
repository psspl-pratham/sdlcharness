import json
from datetime import date
from pathlib import Path

from .agents import AgentOutputError, AgentRuntime
from .blueprints import CONTRACTS, load_blueprint, validate
from .deploy import Deployer
from .models import (
    ARTIFACT_MODELS, Architecture, Backlog, Blueprint, Build, BuildProgress, Deployment, FilePatch,
    Project, QA, Requirements, Retro, SprintPlan, StageRun, now,
)
from .planning import plan
from .providers import provider_info
from .quality import QualityEngine
from .sandbox import Sandbox
from .store import Store
from .workspace import Workspace


class TaskCheckError(RuntimeError):
    """Keep the failed task and passed commits available for an explicit repair."""


class Orchestrator:
    def __init__(self, store: Store, gateway, sandbox=None, deployer=None, max_model_calls=40):
        self.store = store
        self.gateway = gateway
        self.sandbox = sandbox or Sandbox("docker")
        self.max_model_calls = max_model_calls
        if self.sandbox.mode == "local" and self.gateway.name == "bedrock":
            raise ValueError("Live providers require the Docker sandbox")
        self.quality = QualityEngine(self.sandbox)
        self.deployer = deployer or Deployer()

    def create(self, idea: str, blueprint: str | Blueprint = "default", start_date: date | None = None):
        blueprint = load_blueprint(blueprint) if isinstance(blueprint, str) else validate(blueprint)
        if self.gateway.name == "bedrock":
            for stage in blueprint.stages:
                if stage.enabled and stage.name == "deploy" and stage.config.get("target", "local") != "docker":
                    raise ValueError("Bedrock projects require a Docker deploy target. Use the default blueprint or configure target: docker.")
        project = self.store.create(idea, blueprint, start_date or date.today(),
                                    provider=provider_info(self.gateway), max_model_calls=self.max_model_calls)
        workspace = Workspace(self.store.directory(project.id) / "workspace")
        workspace.initialize()
        return project

    def run(self, project_id: str) -> Project:
        with self.store.lock(project_id):
            return self._run(project_id)

    def _run(self, project_id):
        project = self.store.load(project_id)
        if project.status in {"awaiting_approval", "completed", "failed"}:
            return project
        self.gateway_for(project)  # Fail before execution if a live project's model configuration changed.
        workspace = Workspace(self.store.directory(project_id) / "workspace")
        workspace.initialize()
        project.status, project.error = "running", None
        self.store.save(project)
        for stage in project.blueprint.stages:
            run = project.stages[stage.name]
            if run.status in {"done", "skipped"}:
                continue
            if not stage.enabled:
                run.status = "skipped"
                self.store.save(project)
                continue
            if run.status == "running":
                self.store.event(project.id, "stage.recovered", stage.name, "Restarting interrupted stage from its checkpoint")
                run.attempts = max(0, run.attempts - 1)
                if run.workspace_base and (stage.name == "qa" or stage.name == "build" and not run.build_progress):
                    workspace.restore(run.workspace_base)
            if run.workspace_base is None:
                run.workspace_base = workspace.head()
            while run.attempts <= stage.retries:
                if run.attempts and (stage.name == "qa" or stage.name == "build" and not run.build_progress):
                    workspace.restore(run.workspace_base)
                run.attempts += 1
                run.status, run.started = "running", now()
                self.store.save(project)
                self.store.event(project.id, "stage.started", stage.name, attempt=run.attempts)
                try:
                    requires, output = CONTRACTS[stage.name]
                    missing = set(requires) - set(project.artifacts)
                    if missing:
                        raise ValueError(f"Missing inputs: {sorted(missing)}")
                    result = self._execute(project, stage, workspace)
                    project.artifacts[output] = ARTIFACT_MODELS[output].model_validate(result).model_dump(mode="json")
                    snapshot = {output: project.artifacts[output]}
                    if stage.name in {"sprint_plan", "build"}:
                        snapshot["backlog"] = project.artifacts["backlog"]
                    project.stage_artifacts[stage.name] = json.loads(json.dumps(snapshot))
                    run.finished, run.error = now(), None
                    if stage.gate == "manual":
                        run.status, project.status = "awaiting_approval", "awaiting_approval"
                        self.store.save(project)
                        self.store.event(project.id, "gate.waiting", stage.name)
                        self._report(project)
                        return project
                    run.status = "done"
                    self.store.save(project)
                    self.store.event(project.id, "stage.completed", stage.name)
                    break
                except Exception as error:
                    run.status, run.error, run.finished = "failed", str(error), now()
                    self.store.save(project)
                    retrying = not isinstance(error, (AgentOutputError, TaskCheckError)) and run.attempts <= stage.retries
                    self.store.event(project.id, "stage.failed", stage.name, str(error), attempt=run.attempts, retrying=retrying)
                    if not retrying:
                        break
            if run.status == "failed":
                project.status, project.error = "failed", run.error
                self.store.save(project)
                self._report(project)
                return project
        project.status = "completed"
        self.store.save(project)
        self.store.event(project.id, "project.completed")
        self._report(project)
        return project

    def approve(self, project_id: str):
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            stage = self._waiting(project)
            project.stages[stage].status = "done"
            project.status = "created"
            self.store.save(project)
            self.store.event(project.id, "gate.approved", stage)
        return project

    @staticmethod
    def _waiting(project):
        if project.status != "awaiting_approval":
            raise ValueError("Project is not awaiting approval")
        return next(name for name, run in project.stages.items() if run.status == "awaiting_approval")

    def send_back(self, project_id: str, feedback: str):
        if not feedback.strip():
            raise ValueError("Send-back feedback is required")
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            stage = self._waiting(project)
            project.feedback[stage] = feedback.strip()
            self._reset(project, stage)
            self.store.event(project.id, "gate.sent_back", stage, feedback.strip())
        return project

    def rerun(self, project_id: str, stage: str, feedback: str = ""):
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            self._reset(project, stage)
            if feedback.strip():
                project.feedback[stage] = feedback.strip()
                self.store.save(project)
            self.store.event(project.id, "project.rerun", stage, feedback.strip())
        return project

    def repair_task(self, project_id: str, feedback: str = ""):
        """Authorize another bounded repair cycle without resetting passed build work."""
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            self.gateway_for(project)
            run = project.stages.get("build")
            if project.status != "failed" or run is None or run.status != "failed" or "build" in project.artifacts:
                raise ValueError("Only a failed, unfinished build task can be repaired")
            if project.model_calls >= project.max_model_calls:
                raise ValueError("Project model-call limit exhausted")
            workspace = Workspace(self.store.directory(project_id) / "workspace")
            if run.build_progress is None:
                # Upgrade an existing failed run from its audited commits, without generating
                # new code, altering the backlog, or resetting the workspace.
                backlog = Backlog.model_validate(project.artifacts["backlog"])
                active = [t for t in backlog.tasks if t.status == "in_progress"]
                if len(active) != 1:
                    raise ValueError("No unambiguous failed task to resume; use a full stage rerun")
                if not run.started:
                    raise ValueError("Missing build attempt timestamp; use a full stage rerun")
                events = [e for e in self.store.events(project_id, 100_000)
                          if e["stage"] == "build" and e["timestamp"] >= run.started]
                commits = [{"task": e["data"]["task"], "sprint": e["data"]["sprint"],
                            "commit": e["data"]["commit"], "files": e["data"].get("files", []), "notes": ""}
                           for e in events if e["type"] == "task.completed"]
                if {t.id for t in backlog.tasks if t.status == "done"} != {c["task"] for c in commits}:
                    raise ValueError("Passed tasks do not match the commit ledger; use a full stage rerun")
                for commit in commits:
                    workspace.git("merge-base", "--is-ancestor", commit["commit"], "HEAD")
                task = active[0]
                if workspace.git("branch", "--show-current") != f"sprint-{task.sprint}":
                    raise ValueError("Workspace is not on the failed task's sprint branch")
                checks = []
                for event in events:
                    if event["type"] == "quality.quick_check" and event["data"].get("task") == task.id:
                        fields = {k: v for k, v in event["data"].items() if k in QA.model_fields}
                        checks.append({"result": self._history_result(self._repair_result(QA.model_validate(fields)))})
                run.build_progress = BuildProgress(
                    head=workspace.head(), commits=commits, task=task.id, sprint=task.sprint,
                    phase="check" if checks else "generate",
                    completed_sprints=[e["data"]["sprint"] for e in events if e["type"] == "sprint.completed"],
                    changed_files=workspace.files(include_untracked=True), repair_history=checks[-5:],
                )
            progress = run.build_progress
            if not progress.task:
                raise ValueError("There is no failed task to repair")
            try:
                self._check_build_checkpoint(workspace, progress)
            except TaskCheckError as error:
                raise ValueError(str(error)) from None
            progress.repair_attempts = 0
            for item in progress.repair_history:
                item["result"] = self._history_result(item["result"])
            run.status, run.attempts, run.error = "pending", 0, None
            project.status, project.error = "created", None
            if feedback.strip():
                project.feedback["build"] = feedback.strip()
            self.store.save(project)
            self.store.event(project_id, "task.resume_requested", "build", task=progress.task,
                             preserved_tasks=len(progress.commits), feedback=feedback.strip())
        return project

    @staticmethod
    def _repair_result(check):
        result = check.model_dump()
        result["output"] = result["output"][-8000:]
        result["failures"] = result["failures"][:8]
        return result

    @staticmethod
    def _history_result(result):
        """Keep prior outcomes, not repeated framework tracebacks, in model context."""
        result = dict(result)
        result["output"] = "\n".join(line for line in result["output"].splitlines()
                                       if line.startswith(("FAILED ", "ERROR ")))[-1500:]
        result["failures"] = [{"test": f["test"], "message": f["message"][:500]} for f in result.get("failures", [])[:8]]
        return result

    @staticmethod
    def _check_build_checkpoint(workspace, progress):
        if workspace.head() != progress.head:
            raise TaskCheckError("Workspace HEAD differs from the saved build checkpoint; inspect changes or rerun the build")
        if progress.sprint is not None and workspace.git("branch", "--show-current") != f"sprint-{progress.sprint}":
            raise TaskCheckError("Workspace branch differs from the saved sprint; inspect changes or rerun the build")

    def _build(self, project, stage, workspace, agent, emit, feedback):
        artifacts, run = project.artifacts, project.stages[stage.name]
        backlog = Backlog.model_validate(artifacts["backlog"])
        if run.build_progress is None:
            run.build_progress = BuildProgress(head=workspace.head())
        progress = run.build_progress
        self._check_build_checkpoint(workspace, progress)
        completed = {c["task"] for c in progress.commits}
        for task in backlog.tasks:
            task.status = "done" if task.id in completed else "todo"
        tasks = {t.id: t for t in backlog.tasks}
        sprint_plan = SprintPlan.model_validate(artifacts["sprint_plan"])
        max_fixes = stage.config.get("max_fix_attempts", 3)
        emit("sandbox.selected", mode=self.sandbox.mode)

        def save():
            artifacts["backlog"] = backlog.model_dump(mode="json")
            self.store.save(project)

        save()
        for sprint in sprint_plan.sprints:
            if sprint.number in progress.completed_sprints:
                continue
            branch = f"sprint-{sprint.number}"
            if progress.sprint != sprint.number:
                workspace.git("checkout", "-B", branch)
                progress.sprint = sprint.number
                save()
                emit("sprint.started", sprint=sprint.number, goal=sprint.goal, tasks=sprint.tasks,
                     capacity=sprint.capacity, committed=sprint.committed)
            for task_id in sprint.tasks:
                if task_id in completed:
                    continue
                task = tasks[task_id]
                if any(tasks[d].status != "done" for d in task.dependencies):
                    raise TaskCheckError(f"Task {task_id} has an unfinished dependency; review the sprint plan")
                resumed = progress.task == task_id
                if not resumed:
                    progress.task, progress.phase = task_id, "generate"
                    progress.changed_files, progress.repair_history = [], []
                    progress.repair_attempts, progress.notes = 0, ""
                task.status = "in_progress"
                save()
                emit("task.resumed" if resumed else "task.started", task=task_id, sprint=sprint.number,
                     title=task.title, assignee=task.assignee, role=task.role, files=task.files)
                if progress.phase == "generate":
                    patch = agent.produce("developer", f"build:{task_id}", {
                        "idea": project.idea, "task": task.model_dump(), "architecture": artifacts["architecture"],
                        "brief": artifacts["brief"], "files": workspace.context(workspace.files() + task.files + ["app.py"]),
                        "stories": [s for s in artifacts["requirements"]["stories"] if s["id"] in task.story_ids],
                    }, FilePatch, feedback)
                    workspace.apply(patch, "developer")
                    progress.changed_files, progress.notes = sorted(patch.files), patch.notes
                    progress.phase = "check"
                    save()
                    emit("tool.write_files", role="developer", task=task_id, files=sorted(patch.files), notes=patch.notes)
                while True:
                    emit("quality.started", task=task_id, attempt=progress.repair_attempts + 1, mode=self.sandbox.mode)
                    check = self.quality.check(workspace.root)
                    emit("quality.quick_check", task=task_id, attempt=progress.repair_attempts + 1, **check.model_dump())
                    if check.green:
                        break
                    result = self._repair_result(check)
                    previous = progress.repair_history[-1]["result"] if progress.repair_history else None
                    def signature(value):
                        return ([(f["test"], f["message"].split('\n')[0]) for f in value.get("failures", [])],
                                value.get("compile_errors"), value.get("dependency_errors"),
                                [line for line in value["output"].splitlines() if line.startswith(("FAILED ", "ERROR "))])
                    unchanged = bool(previous and signature(result) == signature(previous))
                    progress.repair_history.append({"result": self._history_result(result), "previous_patch_notes": progress.notes[:1500]})
                    progress.repair_history = progress.repair_history[-5:]
                    save()
                    if progress.repair_attempts >= max_fixes:
                        raise TaskCheckError(f"Quick check failed for {task_id} after {progress.repair_attempts} repair attempt(s). "
                                             f"Passed tasks are preserved.\n{check.output[-4000:]}")
                    progress.repair_attempts += 1
                    save()  # Reserve the attempt before a request; restarting cannot replenish it.
                    emit("task.repairing", task=task_id, attempt=progress.repair_attempts, limit=max_fixes,
                         failed=check.failed, errors=check.errors, unchanged=unchanged,
                         failing_tests=[f["test"] for f in check.failures])
                    patch = agent.produce("qa", f"repair:{task_id}", {
                        "idea": project.idea, "task": task.model_dump(), "qa": result,
                        "repair_history": progress.repair_history,
                        "files": workspace.context(workspace.files(include_untracked=True) + task.files),
                        "requirements": artifacts["requirements"], "architecture": artifacts["architecture"],
                    }, FilePatch, feedback)
                    workspace.apply(patch, "qa")
                    progress.changed_files = sorted(set(progress.changed_files) | set(patch.files))
                    progress.notes = patch.notes
                    save()
                    emit("tool.write_files", role="qa", task=task_id, files=sorted(patch.files), notes=patch.notes)
                commit = workspace.commit(f"{task_id}: {task.title}")
                progress.commits.append({"task": task_id, "sprint": sprint.number, "commit": commit,
                                         "files": progress.changed_files, "notes": progress.notes})
                progress.head, progress.task = commit, None
                task.status = "done"
                completed.add(task_id)
                save()
                emit("task.completed", task=task_id, commit=commit, title=task.title,
                     files=progress.changed_files, passed=check.passed, sprint=sprint.number)
            workspace.git("checkout", "main")
            workspace.git("merge", "--no-ff", branch, "-m", f"Merge sprint {sprint.number}")
            progress.head, progress.sprint = workspace.head(), None
            progress.completed_sprints.append(sprint.number)
            save()
            emit("sprint.completed", sprint=sprint.number)
        return Build(files=workspace.files(), commits=progress.commits, commit=workspace.head())

    def _reset(self, project: Project, stage: str):
        names = [s.name for s in project.blueprint.stages]
        if stage not in names:
            raise ValueError(f"Unknown stage: {stage}")
        index = names.index(stage)
        if any(project.stages[name].status not in {"done", "skipped"} for name in names[:index]):
            raise ValueError("Earlier stages must be completed before rerunning this stage")
        reset = names[index:]
        if "deploy" in reset and "deployment" in project.artifacts:
            self.deployer.stop(Deployment.model_validate(project.artifacts["deployment"]))
        # Restore the earliest affected workspace checkpoint, removing stale generated files.
        base = next((project.stages[n].workspace_base for n in reset
                     if n in {"build", "qa"} and project.stages[n].workspace_base), None)
        if base:
            Workspace(self.store.directory(project.id) / "workspace").restore(base)
        for name in reset:
            project.stages[name] = StageRun()
            project.stage_artifacts.pop(name, None)
        project.artifacts = {}
        for name in names[:index]:
            project.artifacts.update(json.loads(json.dumps(project.stage_artifacts.get(name, {}))))
        project.status, project.error = "created", None
        self.store.save(project)
        self._report(project)

    def stop_deployment(self, project_id: str):
        with self.store.lock(project_id):
            project = self.store.load(project_id)
            if "deployment" not in project.artifacts:
                raise ValueError("Project has no deployment")
            deployment = Deployment.model_validate(project.artifacts["deployment"])
            self.deployer.stop(deployment)
            project.artifacts["deployment"] = deployment.model_dump(mode="json")
            if "deploy" in project.stage_artifacts:
                project.stage_artifacts["deploy"]["deployment"] = project.artifacts["deployment"]
            self.store.save(project)
            self.store.event(project_id, "deploy.stopped")
            self._report(project)
        return project

    def _execute(self, project, stage, workspace):
        emit = lambda kind, message="", **data: self.store.event(project.id, kind, stage.name, message, **data)
        gateway = self.gateway_for(project)

        def reserve_call(role, task):
            if project.model_calls >= project.max_model_calls:
                raise RuntimeError(f"Project model-call limit ({project.max_model_calls}) exhausted; reruns do not reset this limit")
            project.model_calls += 1
            self.store.save(project)
            emit("agent.requested", role=role, task=task, provider=gateway.name,
                 model_id=project.provider.model_id, call_number=project.model_calls)

        agent = AgentRuntime(gateway, emit, reserve_call=reserve_call)
        artifacts = project.artifacts
        feedback = project.feedback.get(stage.name, "")
        if stage.name != "intake" and project.feedback.get("intake"):
            feedback = "Product scope clarification: " + project.feedback["intake"] + "\n" + feedback
        if project.stages[stage.name].error:
            feedback += "\nPrevious stage attempt failed; address this observed error: " + project.stages[stage.name].error[-4000:]
        if stage.name in {"intake", "requirements", "architecture", "breakdown"}:
            role = {"intake": "product_manager", "requirements": "product_manager",
                    "architecture": "architect", "breakdown": "tech_lead"}[stage.name]
            inputs = {"idea": project.idea, **{key: artifacts[key] for key in CONTRACTS[stage.name][0]}}
            return agent.produce(role, stage.name, inputs, ARTIFACT_MODELS[CONTRACTS[stage.name][1]], feedback)
        if stage.name == "sprint_plan":
            backlog, sprint_plan = plan(Backlog.model_validate(artifacts["backlog"]), project.blueprint.team,
                                        project.start_date, Requirements.model_validate(artifacts["requirements"]))
            artifacts["backlog"] = backlog.model_dump(mode="json")
            for repair in sprint_plan.repairs:
                emit("planning.repair", message=repair)
            for warning in sprint_plan.warnings:
                emit("planning.warning", message=warning)
            emit("planning.completed", feedback=feedback, sprints=len(sprint_plan.sprints),
                 tasks=len(backlog.tasks), projected_end=str(sprint_plan.projected_end),
                 committed=sum(s.committed for s in sprint_plan.sprints))
            return sprint_plan
        if stage.name == "build":
            return self._build(project, stage, workspace, agent, emit, feedback)
        if stage.name == "qa":
            for attempt in range(stage.config.get("max_fix_attempts", 2) + 1):
                emit("quality.started", attempt=attempt + 1, mode=self.sandbox.mode)
                qa = self.quality.check(workspace.root)
                qa.attempts = attempt + 1
                artifacts["qa"] = qa.model_dump(mode="json")
                self.store.save(project)
                emit("quality.checked", **qa.model_dump())
                if qa.green:
                    return qa
                if attempt < stage.config.get("max_fix_attempts", 2):
                    patch = agent.produce("qa", "repair", {
                        "idea": project.idea,
                        "qa": qa.model_dump(), "files": workspace.context(workspace.files()),
                        "requirements": artifacts["requirements"], "architecture": artifacts["architecture"],
                    }, FilePatch, feedback)
                    workspace.apply(patch, "qa")
                    emit("tool.write_files", role="qa", files=sorted(patch.files))
                    workspace.commit(f"QA: repair attempt {attempt + 1}")
            raise RuntimeError(f"Quality gate failed: {qa.failed} failures, {qa.errors} errors, {len(qa.compile_errors)} compile errors, {len(qa.dependency_errors)} dependency errors")
        if stage.name == "deploy":
            if not artifacts["qa"]["green"]:
                raise ValueError("Cannot deploy a red QA result")
            if "deployment" in artifacts:
                # A previous attempt may have launched a service before the stage finished.
                self.deployer.stop(Deployment.model_validate(artifacts["deployment"]))

            def checkpoint(record):
                artifacts["deployment"] = record.model_dump(mode="json")
                self.store.save(project)

            deploy_config = dict(stage.config)
            if project.provider.name == "bedrock":
                if deploy_config.get("target") != "docker":
                    raise ValueError("Live-generated services require Docker deployment")
                if deploy_config.pop("fallback", None):
                    emit("deploy.safety", message="Local deployment fallback is disabled for live-generated code")
            record = self.deployer.deploy(workspace, Architecture.model_validate(artifacts["architecture"]),
                                          self.store.directory(project.id), deploy_config, emit, checkpoint=checkpoint)
            artifacts["deployment"] = record.model_dump(mode="json")
            self.store.save(project)
            if record.status != "healthy":
                raise RuntimeError(f"Deployment unhealthy: {record.logs[-4000:]}")
            return record
        if stage.name == "retro":
            return agent.produce("scrum_master", "retro", {"metrics": {
                "tasks_done": sum(t["status"] == "done" for t in artifacts["backlog"]["tasks"]),
                "sprints": len(artifacts["sprint_plan"]["sprints"]),
                "tests_passed": artifacts["qa"]["passed"], "qa_attempts": artifacts["qa"]["attempts"],
                "deployment_status": artifacts["deployment"]["status"],
            }, "idea": project.idea}, Retro, feedback)
        raise ValueError(f"No handler for {stage.name}")

    def gateway_for(self, project):
        if project.provider != provider_info(self.gateway):
            raise ValueError("This project's provider/model/region differs from the running configuration. Historical mock projects are read-only; create a new Bedrock project, or use a live project's saved Bedrock settings.")
        return self.gateway

    def _report(self, project):
        lines = [f"# {project.artifacts.get('brief', {}).get('name', project.id)}", "",
                 f"Idea: {project.idea}", f"Status: {project.status}",
                 f"Provider: {project.provider.name}; model: {project.provider.model_id or 'offline fixture'}; region: {project.provider.region or 'local'}",
                 f"Model calls: {project.model_calls}/{project.max_model_calls}", "", "## Stages", ""]
        lines += [f"- {name}: {run.status} (attempts: {run.attempts})" for name, run in project.stages.items()]
        if project.error:
            lines += ["", "## Error", "", project.error]
        for name, artifact in project.artifacts.items():
            lines += ["", f"## {name}", "", "```json", json.dumps(artifact, indent=2), "```"]
        (self.store.directory(project.id) / "REPORT.md").write_text("\n".join(lines) + "\n")
