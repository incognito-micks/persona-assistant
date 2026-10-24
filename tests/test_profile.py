import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from quart import Quart

from admin.settings_cosmosdb import get_current_settings
from config import (
    CONFIG_ADMIN_SETTINGS_CACHE,
    CONFIG_COSMOS_SETTINGS_CONTAINER,
    CONFIG_PROFILE,
)
from core import profile as profile_module
from core.profile import (
    DEFAULT_PROFILE,
    build_persona_instructions,
    get_profile_path,
    load_profile,
    validate_profile_update,
)


def test_profile_loads_validated_partial_configuration_without_mutating_defaults(tmp_path):
    source = tmp_path / "persona.json"
    original = deepcopy(DEFAULT_PROFILE)
    source.write_text(
        json.dumps({"owner_name": "Jamie Example", "assistant_name": "Jamie's assistant"}), encoding="utf-8"
    )
    profile = load_profile(str(source))
    assert profile["owner_name"] == "Jamie Example"
    assert profile["avatar_url"] == "/avatar.svg"
    profile["fun_facts"].append("New fact")
    assert DEFAULT_PROFILE == original


@pytest.mark.parametrize(
    "values",
    [
        [],
        {"id": "override"},
        {"owner_name": ""},
        {"assistant_name": " "},
        {"calendar_url": "javascript:alert(1)"},
        {"avatar_url": "//external.example/avatar.svg"},
        {"avatar_url": "/../private"},
        {"social_image_url": "https://user:password@example.com/image"},
        {"linkedin_url": "http://example.com"},
        {"example_questions": []},
        {"example_questions": [""]},
        {"example_questions": ["Question"] * 9},
        {"example_questions": [1]},
        {"vacation_mode": "true"},
        {"vacation_return_date": "2026-99-99"},
    ],
)
def test_profile_invalid_input_fails_explicitly(tmp_path, values):
    source = tmp_path / "persona.json"
    source.write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(str(source))


def test_profile_missing_oversized_and_malformed_files_fail(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(str(tmp_path / "missing.json"))
    source = tmp_path / "persona.json"
    source.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="64 KiB"):
        load_profile(str(source))
    source.write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_profile(str(source))


def test_profile_path_is_explicit_or_selects_checkout():
    assert get_profile_path().is_file()
    assert get_profile_path("relative-profile.json") == get_profile_path().parent / "relative-profile.json"
    with pytest.raises(ValueError, match="PROFILE_PATH"):
        get_profile_path(" ")


def test_relative_profile_selection_is_independent_of_working_directory(monkeypatch, tmp_path):
    backend_dir = tmp_path / "app/backend"
    backend_dir.mkdir(parents=True)
    (tmp_path / "azure.yaml").write_text("name: persona-assistant", encoding="utf-8")
    monkeypatch.setattr(profile_module, "__file__", str(backend_dir / "core/profile.py"))
    monkeypatch.chdir(backend_dir)
    assert get_profile_path("profile.local.json") == tmp_path / "profile.local.json"


@pytest.mark.asyncio
async def test_profile_precedence_defaults_then_file_then_persisted_fields():
    application = Quart(__name__)
    application.config[CONFIG_PROFILE] = DEFAULT_PROFILE | {
        "owner_name": "File Owner",
        "assistant_name": "File Assistant",
        "example_questions": ["A configured example?"],
    }
    application.config[CONFIG_ADMIN_SETTINGS_CACHE] = {}
    async with application.app_context():
        settings = await get_current_settings()
        assert settings["owner_name"] == "File Owner"
        assert settings["assistant_name"] == "File Assistant"
        application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = Mock(
            read_item=AsyncMock(return_value={"id": "admin_settings", "owner_name": "Persisted Owner"})
        )
        settings = await get_current_settings()
        assert settings["owner_name"] == "Persisted Owner"
        assert settings["assistant_name"] == "File Assistant"
        assert settings["example_questions"] == ["A configured example?"]


def test_persona_text_is_shared_and_explicit_about_ai_identity():
    profile = DEFAULT_PROFILE | {
        "owner_name": "Jamie Example",
        "assistant_name": "Jamie's assistant",
        "tone": "Brief and factual.",
        "vacation_mode": True,
        "vacation_return_date": "2027-01-01",
        "manager_email": "manager@example.com",
        "calendar_url": "https://example.com/book",
    }
    instructions = build_persona_instructions(profile)
    assert "representing Jamie Example" in instructions
    assert "not the actual person" in instructions
    assert "Brief and factual." in instructions
    assert "2027-01-01" in instructions
    assert "manager@example.com" in instructions
    assert "https://example.com/book" in instructions


def test_profile_accepts_safe_optional_links_and_returns_independent_data():
    values = {
        "owner_email": "",
        "calendar_url": "",
        "avatar_url": "https://example.com/avatar.svg",
        "fun_facts": ["Fact"],
    }
    validated = validate_profile_update(values)
    validated["fun_facts"].append("Other")
    assert values["fun_facts"] == ["Fact"]
