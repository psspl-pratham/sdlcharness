from pathlib import Path

import yaml

from .models import Blueprint

# The registry is the source of truth for each stage's input/output contract.
CONTRACTS = {
    "intake": ([], "brief"),
    "requirements": (["brief"], "requirements"),
    "architecture": (["brief", "requirements"], "architecture"),
    "breakdown": (["requirements", "architecture"], "backlog"),
    "sprint_plan": (["requirements", "backlog"], "sprint_plan"),
    "build": (["brief", "requirements", "architecture", "backlog", "sprint_plan"], "build"),
    "qa": (["build", "requirements", "architecture"], "qa"),
    "deploy": (["build", "qa", "architecture"], "deployment"),
    "retro": (["backlog", "sprint_plan", "qa", "deployment"], "retro"),
}
BLUEPRINT_DIR = Path(__file__).parent / "blueprints"


def validate(blueprint: Blueprint) -> Blueprint:
    seen, produced = set(), set()
    for stage in blueprint.stages:
        if stage.name not in CONTRACTS or stage.name in seen:
            raise ValueError(f"Unknown or duplicate stage: {stage.name}")
        seen.add(stage.name)
        if not stage.enabled:
            continue
        requires, output = CONTRACTS[stage.name]
        missing = set(requires) - produced
        if missing:
            raise ValueError(f"Stage {stage.name} requires earlier artifacts: {sorted(missing)}")
        allowed = {"build": {"max_fix_attempts"}, "qa": {"max_fix_attempts"}, "deploy": {"target", "fallback"}}.get(stage.name, set())
        if set(stage.config) - allowed:
            raise ValueError(f"Unknown config for {stage.name}: {sorted(set(stage.config) - allowed)}")
        if stage.name in {"build", "qa"}:
            fixes = stage.config.get("max_fix_attempts", 2)
            if type(fixes) is not int or not 0 <= fixes <= 5:
                raise ValueError("max_fix_attempts must be an integer from 0 to 5")
        if stage.name == "deploy":
            if stage.config.get("target", "local") not in {"local", "docker"}:
                raise ValueError("Deploy target must be local or docker")
            if stage.config.get("fallback") not in {None, "local"}:
                raise ValueError("Deploy fallback must be local")
        produced.add(output)
    return blueprint


def load_blueprint(name_or_path: str = "default") -> Blueprint:
    path = BLUEPRINT_DIR / f"{name_or_path}.yaml" if name_or_path in names() else Path(name_or_path)
    return validate(Blueprint.model_validate(yaml.safe_load(path.read_text())))


def names() -> list[str]:
    return sorted(p.stem for p in BLUEPRINT_DIR.glob("*.yaml"))
