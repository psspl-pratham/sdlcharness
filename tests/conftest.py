"""Keep offline tests isolated from the developer's real .env and AWS credentials."""
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(("HARNESS_", "AWS_")) and name != "HARNESS_DOCKER_TESTS":
            monkeypatch.delenv(name)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("HARNESS_BEDROCK_MODEL_ID", "test-model")
