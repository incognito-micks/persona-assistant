import os
from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock

import pytest

import load_azd_env


@pytest.fixture
def clean_local_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(load_azd_env, "__file__", str(tmp_path / "app/backend/load_azd_env.py"))
    (tmp_path / "azure.yaml").write_text("name: persona-assistant", encoding="utf-8")
    with mock.patch.dict(os.environ, clear=True):
        yield tmp_path


def test_explicit_model_configuration_does_not_require_azd(monkeypatch, clean_local_environment):
    monkeypatch.setenv("AZURE_OPENAI_SERVICE", "configured-model")
    run = Mock(side_effect=AssertionError("azd must not run for explicit local configuration"))
    monkeypatch.setattr(load_azd_env.subprocess, "run", run)
    load_azd_env.load_azd_env()
    run.assert_not_called()


def test_backend_only_artifact_accepts_explicit_model_configuration(monkeypatch, clean_local_environment):
    monkeypatch.setattr(load_azd_env, "__file__", "/app/load_azd_env.py")
    monkeypatch.setenv("AZURE_OPENAI_SERVICE", "container-smoke")
    run = Mock(side_effect=AssertionError("Explicit container configuration must not invoke azd"))
    monkeypatch.setattr(load_azd_env.subprocess, "run", run)
    load_azd_env.load_azd_env()
    run.assert_not_called()


def test_backend_only_artifact_without_configuration_fails_actionably(monkeypatch, clean_local_environment):
    monkeypatch.setattr(load_azd_env, "__file__", "/app/load_azd_env.py")
    monkeypatch.setattr(load_azd_env.subprocess, "run", Mock(side_effect=FileNotFoundError()))
    with pytest.raises(RuntimeError, match=r"\.env"):
        load_azd_env.load_azd_env()


def test_root_env_loads_without_overriding_existing_values(monkeypatch, clean_local_environment):
    (clean_local_environment / ".env").write_text(
        "AZURE_OPENAI_SERVICE=local-model\nEXAMPLE_LOCAL_VALUE=from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXAMPLE_LOCAL_VALUE", "existing")
    monkeypatch.setattr(load_azd_env.subprocess, "run", Mock(side_effect=AssertionError("No azd needed")))
    load_azd_env.load_azd_env()
    assert os.environ["AZURE_OPENAI_SERVICE"] == "local-model"
    assert os.environ["EXAMPLE_LOCAL_VALUE"] == "existing"


@pytest.mark.parametrize(
    "response",
    [FileNotFoundError(), SimpleNamespace(returncode=1), SimpleNamespace(returncode=0, stdout="[]")],
)
def test_missing_configuration_produces_actionable_error(monkeypatch, clean_local_environment, response):
    run = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
    monkeypatch.setattr(load_azd_env.subprocess, "run", run)
    with pytest.raises(RuntimeError, match=r"\.env"):
        load_azd_env.load_azd_env()
