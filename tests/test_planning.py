from datetime import date

import pytest

from fixtures import FixtureGateway
from sdlc_harness.blueprints import load_blueprint, validate
from sdlc_harness.models import Backlog, Requirements
from sdlc_harness.planning import plan


def inputs():
    gateway = FixtureGateway()
    kwargs = {"role": "tech_lead", "inputs": {}, "prompt": "", "max_tokens": 100, "timeout": 1}
    backlog = Backlog.model_validate(gateway.complete(task="breakdown", **kwargs))
    requirements = Requirements.model_validate(gateway.complete(task="requirements", **kwargs))
    return backlog, load_blueprint().team, date(2026, 11, 18), requirements


def test_plan_is_reproducible_and_respects_capacity_and_dependency_order():
    args = inputs()
    before = args[0].model_dump()
    backlog, result = plan(*args)
    assert result == plan(*args)[1]
    assert args[0].model_dump() == before
    positions = {task: (s.number, i) for s in result.sprints for i, task in enumerate(s.tasks)}
    for task in backlog.tasks:
        assert all(positions[dep] < positions[task.id] for dep in task.dependencies)
    capacity = {m.name: int(m.points_per_sprint * args[1].focus_factor) for m in args[1].members}
    assert all(load <= capacity[name] for s in result.sprints for name, load in s.load.items())
    assert result.critical_path == ["T1", "T2", "T3"]
    assert result.critical_path_points == 7
    assert result.projected_end == date(2026, 12, 1)
    assert any("cross-role" in warning for warning in result.warnings)


def test_repairs_cycles_self_and_unknown_dependencies_and_points():
    backlog, team, start, requirements = inputs()
    backlog.tasks[0].dependencies = ["T2", "T1", "unknown", "T2"]
    backlog.tasks[0].points = 4
    repaired, result = plan(backlog, team, start, requirements)
    assert repaired.tasks[0].points == 3
    assert len(result.repairs) == 5
    assert any("broke cycle" in item for item in result.repairs)
    assert result == plan(backlog, team, start, requirements)[1]


def test_oversized_work_gets_dedicated_sprint():
    backlog, team, start, requirements = inputs()
    backlog.tasks[0].points = 13
    _, result = plan(backlog, team, start, requirements)
    assert result.sprints[0].tasks == ["T1"]
    assert any("split it" in warning for warning in result.warnings)


def test_unstaffed_role_fails_without_infinite_loop():
    backlog, team, start, requirements = inputs()
    team.allow_cross_role = False
    with pytest.raises(ValueError, match="No team member"):
        plan(backlog, team, start, requirements)


def test_blueprint_rejects_missing_dependencies_and_bad_config():
    blueprint = load_blueprint()
    blueprint.stages[1].enabled = False
    with pytest.raises(ValueError, match="requires earlier"):
        validate(blueprint)
    blueprint = load_blueprint()
    next(s for s in blueprint.stages if s.name == "qa").config["max_fix_attempts"] = -1
    with pytest.raises(ValueError, match="max_fix_attempts"):
        validate(blueprint)
