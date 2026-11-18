import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sdlc_harness.api import create_app
from sdlc_harness.cli import main
from sdlc_harness.providers import ProviderSettings, load_environment


def use_dotenv(monkeypatch, content):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("HARNESS_BEDROCK_MODEL_ID", raising=False)
    Path(".env").write_text(content)


def test_dotenv_settings_and_existing_key_name_are_loaded(monkeypatch):
    use_dotenv(monkeypatch, "AWS_REGION=ap-south-1\nHARNESS_BEDROCK_MODEL_ID=test-profile\n"
               "BEDROCK_API_KEY='private-test-key-with-$-characters'\n")
    settings = ProviderSettings.from_env()
    assert (settings.provider, settings.region, settings.model_id, settings.sandbox) == (
        "bedrock", "ap-south-1", "test-profile", "docker")
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "private-test-key-with-$-characters"
    assert "private-test-key" not in settings.model_dump_json()


def test_terminal_then_cli_override_dotenv_without_overriding_sdk_token(monkeypatch):
    Path(".env").write_text("AWS_REGION=ap-south-1\nHARNESS_BEDROCK_MODEL_ID=file-model\n"
                           "BEDROCK_API_KEY=file-key\nAWS_BEARER_TOKEN_BEDROCK=file-sdk-key\n")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "terminal-key")
    assert ProviderSettings.from_env().model_id == "test-model"
    explicit = ProviderSettings.from_env(region="us-west-2", model_id="explicit-model")
    assert (explicit.region, explicit.model_id) == ("us-west-2", "explicit-model")
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "terminal-key"


def test_standard_sdk_token_takes_precedence_over_alias():
    Path(".env").write_text("BEDROCK_API_KEY=alias-key\nAWS_BEARER_TOKEN_BEDROCK=sdk-key\n")
    load_environment()
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "sdk-key"


@pytest.mark.parametrize("content", ["", "# no configuration yet\n"])
def test_missing_bedrock_settings_fail_instead_of_falling_back(monkeypatch, content):
    use_dotenv(monkeypatch, content)
    with pytest.raises(ValueError, match="HARNESS_BEDROCK_MODEL_ID"):
        ProviderSettings.from_env()


def test_missing_env_file_and_missing_region_fail_clearly(monkeypatch):
    monkeypatch.delenv("AWS_REGION")
    assert not Path(".env").exists()
    with pytest.raises(ValueError, match="AWS_REGION"):
        ProviderSettings.from_env()


def test_dotenv_does_not_execute_shell_or_expand_secret_text(monkeypatch):
    Path(".env").write_text('BEDROCK_API_KEY="$(touch sentinel)${UNSET_VALUE}"\n')
    load_environment()
    assert not Path("sentinel").exists()
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "$(touch sentinel)${UNSET_VALUE}"


def test_loader_only_reads_the_current_working_directory(tmp_path, monkeypatch):
    Path(".env").write_text("BEDROCK_API_KEY=parent-key\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    load_environment()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_cli_loads_dotenv_before_resolving_data_directory(monkeypatch, capsys):
    Path(".env").write_text("HARNESS_DATA_DIR=custom-projects\nBEDROCK_API_KEY=private-key\n")
    monkeypatch.setattr(sys, "argv", ["harness", "list"])
    main()
    assert Path("custom-projects").is_dir()
    assert json.loads(capsys.readouterr().out) == []


def test_api_factory_loads_dotenv_before_resolving_settings_and_storage(monkeypatch):
    use_dotenv(monkeypatch, "HARNESS_DATA_DIR=custom-projects\nAWS_REGION=ap-south-1\n"
               "HARNESS_BEDROCK_MODEL_ID=test-profile\nBEDROCK_API_KEY=private-key\n")
    app = create_app()
    with TestClient(app) as client:
        payload = client.get("/health").json()
    assert payload["provider"] == "bedrock" and payload["model_id"] == "test-profile"
    assert payload["region"] == "ap-south-1" and payload["sandbox"] == "docker"
    assert app.state.store.root == Path("custom-projects").resolve()
    assert "private-key" not in json.dumps(payload)


def test_cli_rejects_mock_provider(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["harness", "--provider", "mock", "doctor"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
