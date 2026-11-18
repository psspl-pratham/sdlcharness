import json
import os

import pytest

from sdlc_harness.quality import DEPENDENCY_SCRIPT, QualityEngine
from sdlc_harness.sandbox import Sandbox


@pytest.mark.parametrize("manifest,expected", [
    ("pydanticv1\n", "pydantic>=2,<3"),
    ("pytest<1\n", "does not match tested sandbox version"),
    ("fastapi[standard]\n", "URL or extras"),
    ("fastapi @ https://private-test-token@example.invalid/package.whl\n", "URL or extras"),
    ("--extra-index-url https://private-test-token@example.invalid\n", "pip options"),
])
def test_bad_manifest_blocks_qa_before_tests_and_does_not_expose_urls(tmp_path, manifest, expected):
    (tmp_path / "requirements.txt").write_text(manifest)
    (tmp_path / "test_sentinel.py").write_text("raise RuntimeError('Tests must not run with an invalid manifest')\n")
    result = QualityEngine(Sandbox()).check(tmp_path)
    assert not result.green and result.dependency_errors
    assert expected in result.output
    assert "private-test-token" not in result.output
    assert "Tests must not run" not in result.output
    assert not (tmp_path / ".harness/junit.xml").exists()


@pytest.mark.parametrize("manifest", [
    "# Dependencies\nfastapi>=0.115,<1 # framework\npydantic>=2,<3\npytest>=8,<10\n",
    'not-an-installed-package-for-this-test; python_version < "2"\n',
    "\ufeffpydantic>=2,<3\n",
])
def test_valid_specs_comments_and_inactive_markers_are_checked_without_installing(tmp_path, manifest):
    (tmp_path / "requirements.txt").write_text(manifest)
    result = Sandbox().run(tmp_path, ["-c", DEPENDENCY_SCRIPT])
    assert result.returncode == 0, result.output
    assert json.loads(result.output) == {"errors": []}


@pytest.mark.skipif(os.environ.get("HARNESS_DOCKER_TESTS") != "1", reason="Requires the Docker sandbox image")
def test_manifest_is_validated_in_the_actual_docker_sandbox(tmp_path):
    (tmp_path / "requirements.txt").write_text("pydanticv1\n")
    result = QualityEngine(Sandbox("docker")).check(tmp_path)
    assert result.dependency_errors and "pydanticv1" in result.output
    assert not result.green and result.passed == 0
