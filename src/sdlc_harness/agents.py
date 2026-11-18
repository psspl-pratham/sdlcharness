import json
from time import perf_counter
from typing import Protocol

from pydantic import BaseModel, ValidationError

from .models import ARTIFACT_MODELS
from .providers import GatewayResponse

PERSONAS = {
    "product_manager": "Use the actual product idea to define a small feasible product. Apply reviewer feedback. Produce only the artifact requested by the current task. Do not substitute a generic sample product.",
    "architect": "Design the requested product as one Python 3.12 FastAPI service. Use app.py with a FastAPI instance named app; deployment invokes app:app. Include GET /health (200) and OpenAPI. The POC sandbox provides only the standard library, FastAPI, Pydantic, pytest, httpx, and uvicorn. Use in-memory or sqlite3 storage; no external services or extra packages. Specify API paths, request/response contracts, data model, and decisions precisely.",
    "tech_lead": "Break the actual requirements and architecture into at most 8 small tasks. Include acceptance tests, Dockerfile, requirements.txt, README.md, and CI as delivery work. Use stable task IDs, Fibonacci points, matching epic IDs, story IDs, and explicit dependencies/files. The FIRST task must create a runnable app.py and at least one passing health test. Every task must leave the existing test suite passing. Later API tasks include their own tests. Delivery work depends on the service and its tests. Restrict dependencies to the sandbox's installed packages.",
    "developer": "Implement only the assigned task and its acceptance criteria in the supplied architecture. Return a files mapping of workspace-relative paths to COMPLETE replacement file contents, not diffs. Preserve existing behavior and tests. The first task must create app.py with app=FastAPI(), GET /health, and a passing pytest test. Use pytest tests named test_*.py, with FastAPI TestClient for HTTP checks. The POC has only Python standard library, FastAPI/Pydantic, httpx, pytest, and uvicorn installed. No pip installs, shell scripts, secrets, or external services at runtime. When packaging, the Dockerfile must start python -m uvicorn app:app --host 0.0.0.0 --port 8000. Include current service dependencies in requirements.txt.",
    "qa": "Debug the specific observed failures using the current files, structured test failures, and repair history. Make the smallest changes that fix their cause. Prior repair notes are claims; only the latest test results prove a fix. If the same failure remains, correct the actual failing code rather than repeat the previous claim. Preserve acceptance criteria and existing tests; do not delete tests, weaken assertions, mark tests skipped, or fabricate green output. Keep tests in test files. Shared in-memory state needs a function-scoped pytest fixture that clears the real app state before and after each test. When fixing Python scope errors, inspect assignments as well as reads; mutate a shared collection in place or explicitly declare its scope. Return a files mapping with COMPLETE replacement contents. Use only the sandbox's installed packages. Do not add authentication, packages, endpoints, or unrelated features to repair an existing failure. Keep app.py/app:app deployment compatible.",
    "scrum_master": "Explain delivery outcomes using the measured metrics provided.",
}
PRODUCT_SCOPE = (
    " Treat inputs.idea as the source of truth throughout the run, even if an earlier artifact lost detail. "
    "Preserve the requested product, domain nouns, user actions, UI needs, and numeric limits in the current artifact; "
    "never replace them with a generic Resource API. Do only the current stage's work."
)
DELIVERY_SCOPE = (
    " For a requested web page, include the actual usable HTML/CSS/JavaScript "
    "interface served at GET / by FastAPI using HTMLResponse or FileResponse (no Node build or Jinja dependency). "
    "User stories and acceptance tests must describe the user's actions and visible results, beyond infrastructure checks. "
    "Keep the design to one service and combine small delivery chores rather than allocating a task to each file. "
    "Tests must create or seed real application state through its public API or an actual shared fixture. "
    "Only override a callable that the endpoint actually declares with Depends; an unrelated lambda is not an override. "
    "Use Pydantic model_dump(). Keep test code in test files, not app.py."
    " requirements.txt must use actual package distribution names and version constraints compatible with the "
    "tested sandbox. Use pydantic>=2,<3, never pydanticv1; pydantic.v1 is an import namespace, not a package name."
)
STAGE_SCOPE = {
    "intake": "Summarize the idea as a brief: product name, problem, intended users, domain entities, and constraints. Preserve explicit limits in constraints. Do not write requirements, user stories, architecture, endpoints, dependencies, code, or HTML yet.",
    "requirements": "Write testable user stories and nonfunctional requirements only. Cover the requested user actions and visible results, API input validation, missing-resource behavior, and GET /health. Do not write code, HTML, a dependency manifest, or a task plan yet.",
    "architecture": "Design only the architecture artifact; describe the interface and API contracts without writing implementation code or a task plan.",
    "breakdown": "Return only the backlog of epics and tasks. Refer to the supplied requirements and architecture; do not generate file contents or a sprint schedule.",
}
TOOLS = {
    "product_manager": (), "architect": (), "tech_lead": (),
    "developer": ("read_files", "write_files", "quick_check"),
    "qa": ("read_files", "write_files", "run_tests"),
    "scrum_master": ("planning_engine",),
}


class Gateway(Protocol):
    name: str

    def complete(self, *, role: str, task: str, inputs: dict, prompt: str, max_tokens: int, timeout: int) -> dict | str | GatewayResponse: ...


class AgentOutputError(ValueError):
    """The artifact failed validation twice; do not repeat the whole stage automatically."""


class AgentRuntime:
    def __init__(self, gateway: Gateway, emit, reserve_call=None):
        self.gateway, self.emit = gateway, emit
        self.reserve_call = reserve_call

    def produce(self, role: str, task: str, inputs: dict, schema: type[BaseModel], feedback: str = ""):
        root_fields = list(schema.model_fields)
        wrapper = next((key for key, model in ARTIFACT_MODELS.items() if model is schema), None)
        contract = (f"Return only one {schema.__name__} JSON object. Its root fields are: "
                    f"{', '.join(root_fields)}. These fields belong directly at the root. "
                    "Do not wrap the object in an artifact name, add fields from other stages, or include commentary.")
        payload = {
            "response_contract": contract,
            "task": task, "stage_scope": STAGE_SCOPE.get(task.split(':')[0], "Complete only the assigned task."),
            "schema": schema.model_json_schema(),
            "persona": PERSONAS[role] + PRODUCT_SCOPE + (DELIVERY_SCOPE if role in {"architect", "tech_lead", "developer", "qa"} else ""),
            "feedback": feedback, "inputs": inputs,
            "allowed_tools": TOOLS[role],
        }
        for attempt in range(2):
            if self.reserve_call:
                self.reserve_call(role, task)
            usage, tokens, response_format = {}, None, "text"
            metadata = {"model_id": getattr(self.gateway, "model_id", None),
                        "region": getattr(self.gateway, "region", None)}
            started = perf_counter()
            self.emit("agent.started", role=role, task=task, attempt=attempt + 1,
                      input_keys=sorted(inputs), files=sorted(inputs.get("files", {})),
                      output_type=schema.__name__, **metadata)
            try:
                output = self.gateway.complete(
                    role=role, task=task, inputs=inputs, prompt=json.dumps(payload),
                    max_tokens=getattr(self.gateway, "max_tokens", 8000),
                    timeout=getattr(self.gateway, "timeout", 60),
                )
                if isinstance(output, GatewayResponse):
                    usage = output.usage
                    tokens = usage.get("totalTokens")
                    if output.stop_reason not in {"end_turn", "stop_sequence"} and not (output.stop_reason == "tool_use" and output.artifact is not None):
                        raise RuntimeError(f"Bedrock response stopped with {output.stop_reason}; adjust the output-token limit or review the model response policy")
                    if output.artifact is not None:
                        output, response_format = output.artifact, "tool_arguments"
                    else:
                        output = output.text.strip()
                        if output.startswith("```json\n") and output.endswith("```"):
                            output = output[8:-3].strip()
                output = json.loads(output) if isinstance(output, str) else output
                # Accept only an unambiguous, single artifact envelope. Never discard sibling
                # fields from a multi-stage response or relax the artifact's own validation.
                unwrapped_from = None
                if wrapper and isinstance(output, dict) and set(output) == {wrapper} and isinstance(output[wrapper], dict):
                    output, unwrapped_from = output[wrapper], wrapper
                artifact = schema.model_validate(output)
            except (ValidationError, json.JSONDecodeError) as error:
                if isinstance(error, ValidationError):
                    errors = [{"path": ".".join(str(part) for part in e["loc"])[:120] or "$", "type": e["type"]}
                              for e in error.errors(include_input=False, include_context=False, include_url=False)]
                    detail = "; ".join(f"{e['path']} ({e['type']})" for e in errors[:12])
                    if len(errors) > 12:
                        detail += f"; {len(errors) - 12} more errors"
                else:
                    errors = [{"path": "$", "type": "invalid_json"}]
                    detail = f"Invalid JSON at line {error.lineno}, column {error.colno}"
                summary = f"{schema.__name__} response rejected: {detail}. Expected root fields: {', '.join(root_fields)}."
                self.emit("agent.call", role=role, task=task, provider=self.gateway.name,
                          attempt=attempt + 1, outcome="invalid", tokens=tokens, usage=usage,
                          error=summary, validation_errors=errors[:12], output_type=schema.__name__, retrying=attempt == 0, response_format=response_format,
                          duration_ms=round((perf_counter() - started) * 1000), **metadata)
                if attempt:
                    raise AgentOutputError(f"{summary} Stopped after 2 invalid responses; revise or explicitly retry this stage.") from None
                payload["validation_feedback"] = {
                    "summary": "Validation error: " + summary,
                    "instruction": "Regenerate only the requested artifact from the original inputs. " + contract,
                }
            except Exception as error:
                self.emit("agent.call", role=role, task=task, provider=self.gateway.name,
                          attempt=attempt + 1, outcome="failed", error=str(error), tokens=tokens, usage=usage, response_format=response_format,
                          duration_ms=round((perf_counter() - started) * 1000), **metadata)
                raise
            else:
                self.emit("agent.call", role=role, task=task, provider=self.gateway.name,
                          attempt=attempt + 1, outcome="valid", tokens=tokens, usage=usage,
                          unwrapped_from=unwrapped_from, response_format=response_format,
                          duration_ms=round((perf_counter() - started) * 1000), output_type=schema.__name__, **metadata)
                return artifact
