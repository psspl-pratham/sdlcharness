"""Opt in after building Dockerfile.sandbox; the default suite stays offline."""
import os

import pytest

from fixtures import FixtureGateway
from sdlc_harness.blueprints import load_blueprint
from sdlc_harness.orchestrator import Orchestrator
from sdlc_harness.sandbox import Sandbox
from sdlc_harness.store import Store


@pytest.mark.skipif(os.environ.get("HARNESS_DOCKER_TESTS") != "1", reason="Set HARNESS_DOCKER_TESTS=1 with Docker available")
def test_docker_checks_and_deployment(tmp_path):
    runner = Orchestrator(Store(tmp_path), gateway=FixtureGateway(), sandbox=Sandbox("docker"))
    blueprint = load_blueprint("autopilot")
    next(stage for stage in blueprint.stages if stage.name == "deploy").config = {"target": "docker"}
    project = runner.create("Docker sample tracker", blueprint)
    try:
        result = runner.run(project.id)
        assert result.status == "completed", result.error
        assert result.artifacts["qa"]["green"] and result.artifacts["qa"]["passed"] == 6
        assert result.artifacts["deployment"]["target"] == "docker"
        assert result.artifacts["deployment"]["status"] == "healthy"
        assert {check["path"] for check in result.artifacts["deployment"]["checks"]} == {"/health", "/items", "/openapi.json"}
    finally:
        if "deployment" in runner.store.load(project.id).artifacts:
            runner.stop_deployment(project.id)
