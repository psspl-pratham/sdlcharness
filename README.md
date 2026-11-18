# SDLC Harness POC

A single Python service that exercises the workflow in [the architecture document](./SDLC%20Harness%20%E2%80%94%20POC%20Architecture.md): idea → brief → requirements → architecture → backlog → sprint plan → build → QA → deploy → retro. The default blueprint pauses for approval after planning and QA.

The **AWS Bedrock provider** sends the actual idea, role prompts, JSON schemas and feedback to a configured model through Converse. There is no runtime mock mode or fixed-output fallback. See [Bedrock setup](./BEDROCK.md) for configuration, authentication and a small connection test. Tests, git operations, subprocess execution, health checks, and smoke checks are real. Live model quality has not yet been verified against an AWS account.

## Run it

Requires Python 3.12+, git and Docker, on Linux, plus a Bedrock API key or AWS credentials and a model/region. No database or frontend build is needed.

The harness automatically loads `.env` from the working directory. Add your key, region and model there using [.env.example](./.env.example) as a reference; follow [BEDROCK.md](./BEDROCK.md). Existing terminal variables and explicit CLI flags override file settings. Restart after editing the file.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/harness serve
```

Open <http://127.0.0.1:8000>. API documentation is at <http://127.0.0.1:8000/docs>. The server binds to loopback; this POC has no multi-user authentication. For programmatic API writes, send `X-Harness-Request: 1`; the console sends this header automatically.

The console supports project creation, polling stage progress, approval/send-back, task board, sprint assignments and warnings, artifact inspection, source previews, events, reports, reruns, and stopping a deployed service.

The default overview compares your original idea with the model's accepted brief and user stories. Click any pipeline stage to read its responsibility and output. **Sprint plan** shows full task titles, dates, owners, role capacity, dependencies and warnings; those dates are planning estimates, while execution proceeds immediately in sprint order. **Agent activity** translates recorded model requests, file writes, sandbox checks, repairs and commits into readable actions, with the raw event available underneath. Roles use the same configured Bedrock model sequentially.

Failed checks show a short result and a blocked task, with the full test output in an expandable section. The code browser includes uncommitted files from a failed attempt. **Repair failed task & continue** checks the current files, keeps passed task commits, and gives QA another bounded repair cycle before continuing to the next review gate. It uses the project's remaining model-call budget. **Replan from original idea** accepts scope feedback, resets downstream artifacts and code, and returns to the planning gate. Review the brief and stories before approving; schema-valid model output and passing tests do not establish that the model understood the intended product.

## CLI demo

```bash
# Run until the sprint-plan gate. The JSON result includes the project ID.
.venv/bin/harness create "A small item tracker" --start-date 2026-11-18

# Substitute the returned ID for PROJECT_ID.
.venv/bin/harness approve PROJECT_ID
# The run now waits at the QA/release gate.
.venv/bin/harness approve PROJECT_ID

.venv/bin/harness status PROJECT_ID
.venv/bin/harness events PROJECT_ID
.venv/bin/harness stop PROJECT_ID
```

Other operations:

```bash
.venv/bin/harness create "A small item tracker" --blueprint autopilot
.venv/bin/harness create "A small item tracker" --blueprint planning-only
.venv/bin/harness send-back PROJECT_ID "Review the scope and assignments"
.venv/bin/harness rerun PROJECT_ID build
.venv/bin/harness run PROJECT_ID
.venv/bin/harness list
```

`run` resumes an interrupted run. New build checkpoints retain passed tasks, the active task's files, and spent repair attempts across restarts. An exhausted failed task can be continued with **Repair failed task & continue** in the console (`POST /api/projects/ID/repair-task`, body `{"feedback": "optional clarification"}`); the workspace branch and commit checkpoint must match. Older failed builds recover their checkpoint from the task commit ledger. Approval decisions are saved before the next stage starts. A full `rerun PROJECT_ID STAGE` resets the selected stage and everything after it, invalidates downstream artifacts, restores upstream backlog state, and stops an invalidated deployment. Rerunning build or QA restores the relevant git checkpoint and removes generated files from the affected work. It operates only in harness-owned project repositories.

## Blueprints and scheduling

Bundled YAML blueprints live in `src/sdlc_harness/blueprints/`:

- `default`: nine stages, manual planning and QA gates, Docker deploy with local fallback.
- `autopilot`: nine stages, no gates, local deployment.
- `planning-only`: stops at the plan, with a manual gate.

The CLI also accepts a YAML filename through `--blueprint`. The console exposes bundled blueprints only. Stage names resolve to registered input/output contracts; unknown stages, duplicate stages, missing upstream artifacts and unsupported config fail validation before work starts. Add a bundled blueprint or pass a file to change order, gates, retry limits, capacity, cross-role work, and deployment target without changing stage handlers.

The planner uses a persisted start date, Fibonacci point repair, deterministic DFS cycle repair, critical-path tails, role-based capacity, and two-pass greedy packing. Ties use stable IDs and member names. Oversized work gets a dedicated sprint and a split warning; an unstaffed role with cross-role work disabled fails clearly. Repeated inputs and the same start date produce the same plan. Sprint dates are calendar days in this POC.

## Execution modes

Bedrock-generated code requires Docker execution and deployment. The CLI rejects local execution. The local subprocess sandbox remains an internal test utility for inspected test fixtures; it is not a runtime provider or a security boundary.

Docker test execution is available when the Docker engine is accessible:

```bash
docker build -f Dockerfile.sandbox -t sdlc-harness-sandbox:local .
.venv/bin/harness --sandbox docker serve
```

Docker checks use no network, resource limits, dropped capabilities, and a mounted project workspace. Dependencies are installed when building the sandbox image. The image includes the packages needed by the sample service; arbitrary additional dependencies are outside this first slice. Deployment uses the blueprint target independently of the test sandbox. Docker deployment builds the generated Dockerfile and publishes the service on loopback. Only Docker unavailability triggers the configured local fallback; a broken image build fails the deploy stage.

Every build task receives its architecture, brief, and budgeted file context, returns a typed file patch, passes compile/pytest checks, and gets a commit. Each sprint has a branch merged into `main`. QA runs the complete suite and parses JUnit XML; zero tests, a test runner error, compilation failure, or red tests cannot pass. Failed QA may ask the model for a bounded repair and rerun the tests. Deploy uses a release snapshot, checks `/health`, `/openapi.json`, and each parameter-free GET endpoint. A failed replacement is stopped while a previous healthy instance is retained by the deployer.

When `requirements.txt` exists, the quality engine validates its package names and version constraints against distributions installed in the actual test sandbox before compiling or running tests. Missing packages, incompatible constraints, URLs, extras and pip directives fail with dependency errors. Initial build tasks can run before the manifest exists. This catches generated names such as `pydanticv1`; the supported distribution is `pydantic`. The check uses no package installation or network access. Docker deployment still builds the final image and checks the running service.

All model roles receive the original product idea with instructions limited to their current stage. Intake feedback is carried forward to later roles. A single artifact-name wrapper (for example, `{"brief": {...}}`) is removed and recorded before validating the complete artifact; responses with extra sibling fields are rejected. Invalid output gets one corrective model request, then stops the project without repeating the whole stage automatically. Activity records the rejected field paths and whether a retry will follow. An explicit rerun uses the remaining persisted model-call budget.

A failing build task gets up to three QA repair attempts by default. Each attempt receives the current files, individual failing tests and tracebacks from JUnit, and the recent repair history. Tests run after every patch; model notes never establish success. Configure `build.config.max_fix_attempts` from 0 to 5 to change this limit. Exhaustion stops on the failed task without restarting the whole build. An explicit task repair resets that task's repair allowance and preserves the persisted model-call count. Other stage failures use the blueprint retry limits; a failed task cannot be committed or reach deployment. The API's rerun body accepts optional `feedback` alongside `stage`.

## State and recovery

Default storage is `data/projects/PROJECT_ID/`:

```text
state.json          # atomic, fsynced project ledger and accepted stage snapshots
events.jsonl        # audit events: stages, agents, patches, checks, decisions
worker.lock         # cross-process project lock
workspace/          # generated service git repository
releases/COMMIT/    # immutable deployment snapshots
deploy-*.log        # local deployment output
REPORT.md           # current artifacts and outcome
```

Use `--data-dir PATH` before the CLI command or set `HARNESS_DATA_DIR`. `HARNESS_SANDBOX` sets the default execution mode. A blueprint is copied into the ledger when creating a project, so later YAML edits do not silently change an existing run.

Completed stages are skipped. An interrupted stage is restarted from its saved boundary; build and QA restore their workspace checkpoints. This is stage-level recovery, so an interrupted build may repeat tasks rather than continue inside the task. Starting the API recovers projects left `created` or `running`; paused gates remain paused. Only one worker can hold a project's lock, including across CLI and API processes. Local deployments continue after the CLI exits or the console stops; stop them through `harness stop` or the console.

New projects also save their provider, model/region and model-call attempt limit. Historical mock ledgers remain readable but cannot run again. Live runs reject a different model/region on resume. Credential values are resolved by the AWS SDK and are not persisted in project records.

## Verify

```bash
.venv/bin/python -m pytest -q
```

The suite isolates itself from your real `.env` and AWS credentials, using test doubles under `tests/`. It includes actual generated-service subprocess tests and a temporary localhost deployment, in addition to scheduling, blueprint validation, file confinement, locks, retry budgets, gate decisions, reruns, recovery, and the red-QA deployment barrier. The environment must allow local sockets for FastAPI's threaded test client and the deployed service.

After building `Dockerfile.sandbox`, opt into the full Docker execution/deploy test:

```bash
HARNESS_DOCKER_TESTS=1 .venv/bin/python -m pytest tests/test_docker.py -q
```

## Next increments

1. Validate the Bedrock adapter against an authorized AWS account and a small acceptance set of actual product ideas. Stubbed adapter tests are available; no live inference has been performed during implementation.
2. Extend Docker dependency provisioning and isolation before accepting untrusted generated code.
3. Add a small acceptance set of product ideas and verify their requirements through generated tests.

This remains a local POC: no cloud deployment, external Git hosting, queue service, multi-user auth, cost accounting, or parallel developer agents.
