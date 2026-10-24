import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from core import knowledgebase
from core.knowledgebase import get_knowledge_base_path, load_knowledge_base
from package_knowledge import package_knowledge_base


def test_missing_knowledge_directory_fails(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match="Required knowledge base directory not found") as error:
        load_knowledge_base(str(missing))

    assert str(missing) in str(error.value)


def test_knowledge_path_must_be_directory(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("Context", encoding="utf-8")

    with pytest.raises(NotADirectoryError, match="not a directory"):
        load_knowledge_base(str(source))


@pytest.mark.parametrize("files", [{}, {"ignored.txt": "Not markdown"}, {"blank.md": " \n\t"}])
def test_empty_knowledge_fails(tmp_path, files):
    for filename, content in files.items():
        (tmp_path / filename).write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="no nonempty markdown files") as error:
        load_knowledge_base(str(tmp_path))

    assert str(tmp_path) in str(error.value)


def test_knowledge_is_sorted_formatted_and_trimmed(tmp_path):
    for filename, content in {
        "zeta.md": "\nLast source\n",
        "alpha.md": " First source ",
        "blank.md": "\n",
        "ignored.txt": "Not context",
    }.items():
        (tmp_path / filename).write_text(content, encoding="utf-8")

    assert load_knowledge_base(str(tmp_path)) == ["[alpha]: First source", "[zeta]: Last source"]


@pytest.mark.parametrize("read_error", [OSError("Cannot read"), UnicodeError("Invalid UTF-8")])
def test_unreadable_knowledge_fails(tmp_path, monkeypatch, read_error):
    source = tmp_path / "source.md"
    source.write_text("Context", encoding="utf-8")

    def fail_read(*args, **kwargs):
        raise read_error

    monkeypatch.setattr(Path, "read_text", fail_read)

    with pytest.raises(RuntimeError, match="Unable to read required knowledge file") as error:
        load_knowledge_base(str(tmp_path))

    assert str(source) in str(error.value)
    assert error.value.__cause__ is read_error


def test_configured_knowledge_path_takes_precedence(tmp_path):
    configured = str(tmp_path / "external")

    assert get_knowledge_base_path(configured) == configured


def test_relative_knowledge_override_is_independent_of_working_directory(monkeypatch, tmp_path):
    backend_dir = tmp_path / "app/backend"
    backend_dir.mkdir(parents=True)
    (tmp_path / "azure.yaml").write_text("name: persona-assistant", encoding="utf-8")
    monkeypatch.setattr(knowledgebase, "__file__", str(backend_dir / "core/knowledgebase.py"))
    monkeypatch.chdir(backend_dir)
    assert get_knowledge_base_path("private/knowledge") == str(tmp_path / "private/knowledge")


@pytest.mark.parametrize("configured", ["", " \t"])
def test_blank_configured_path_fails(configured):
    with pytest.raises(ValueError, match="KNOWLEDGE_DATA_DIR must not be empty"):
        get_knowledge_base_path(configured)


def test_checkout_uses_source_directory_even_when_missing(tmp_path, monkeypatch):
    backend_dir = tmp_path / "app" / "backend"
    staged_dir = backend_dir / "data" / "work"
    staged_dir.mkdir(parents=True)
    (staged_dir / "stale.md").write_text("Outdated context", encoding="utf-8")
    (tmp_path / "azure.yaml").write_text("name: test", encoding="utf-8")
    monkeypatch.setattr(knowledgebase, "__file__", str(backend_dir / "core" / "knowledgebase.py"))

    resolved = get_knowledge_base_path()

    assert resolved == str(tmp_path / "data" / "work")
    with pytest.raises(FileNotFoundError):
        load_knowledge_base(resolved)


def test_packaging_refreshes_markdown_and_removes_stale_sources(tmp_path):
    source_dir = tmp_path / "sources"
    destination_dir = tmp_path / "package"
    source_dir.mkdir()
    destination_dir.mkdir()
    (source_dir / "current.md").write_text("Current context", encoding="utf-8")
    (source_dir / "ignored.txt").write_text("Not markdown", encoding="utf-8")
    (destination_dir / "current.md").write_text("Old context", encoding="utf-8")
    (destination_dir / "removed.md").write_text("Removed context", encoding="utf-8")

    assert package_knowledge_base(source_dir, destination_dir) == 1
    assert sorted(source.name for source in destination_dir.iterdir()) == ["current.md"]
    assert load_knowledge_base(str(destination_dir)) == ["[current]: Current context"]


def test_packaging_validates_before_modifying_existing_package(tmp_path):
    source_dir = tmp_path / "empty"
    destination_dir = tmp_path / "package"
    source_dir.mkdir()
    destination_dir.mkdir()
    previous_source = destination_dir / "previous.md"
    previous_source.write_text("Previous context", encoding="utf-8")

    with pytest.raises(ValueError, match="no nonempty markdown files"):
        package_knowledge_base(source_dir, destination_dir)

    assert previous_source.read_text(encoding="utf-8") == "Previous context"


@pytest.mark.parametrize("artifact_path", ["container/app", "appservice/site/wwwroot"])
def test_packaged_backend_loads_all_repository_sources_without_checkout(tmp_path, artifact_path):
    repository_dir = Path(__file__).resolve().parents[1]
    checkout_dir = tmp_path / "checkout"
    backend_dir = checkout_dir / "app" / "backend"
    (backend_dir / "core").mkdir(parents=True)
    shutil.copyfile(repository_dir / "app/backend/core/knowledgebase.py", backend_dir / "core/knowledgebase.py")
    shutil.copyfile(repository_dir / "app/backend/core/profile.py", backend_dir / "core/profile.py")
    shutil.copyfile(repository_dir / "app/backend/package_knowledge.py", backend_dir / "package_knowledge.py")
    shutil.copytree(repository_dir / "data/work", checkout_dir / "data/work")
    shutil.copyfile(repository_dir / "profile.json", checkout_dir / "profile.json")
    (checkout_dir / "azure.yaml").write_text("name: persona-assistant", encoding="utf-8")
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}

    packaged = subprocess.run(
        [sys.executable, str(backend_dir / "package_knowledge.py")],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_sources = load_knowledge_base(str(repository_dir / "data/work"))
    assert f"Packaged {len(expected_sources)} knowledge sources" in packaged.stdout

    artifact_dir = tmp_path / artifact_path
    shutil.copytree(backend_dir, artifact_dir)
    shutil.rmtree(checkout_dir)
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from core.knowledgebase import get_knowledge_base_path, load_knowledge_base; "
            "print(json.dumps(load_knowledge_base(get_knowledge_base_path())))",
        ],
        cwd=artifact_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(loaded.stdout) == expected_sources
    assert sorted(source.name for source in (artifact_dir / "data/work").glob("*.md")) == sorted(
        source.name for source in (repository_dir / "data/work").glob("*.md")
    )


def test_azd_hooks_stage_knowledge_and_profile_for_container_deployment():
    repository_dir = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((repository_dir / "azure.yaml").read_text(encoding="utf-8"))
    backend = manifest["services"]["backend"]
    project_dir = (repository_dir / backend["project"]).resolve()

    assert project_dir == repository_dir / "app/backend"
    assert (project_dir / backend["docker"]["context"]).resolve() == project_dir
    assert (project_dir / backend["docker"]["path"]).is_file()
    for event in ("prebuild", "prepackage"):
        for platform in ("posix", "windows"):
            hook = backend["hooks"][event][platform]
            assert hook["run"].index("python package_knowledge.py") < hook["run"].index("npm run build")
            assert hook["continueOnError"] is False

    assert set(manifest["services"]) == {"backend"}
    assert set(manifest["hooks"]) == {"preprovision", "postprovision"}
    for platform, extension in (("posix", "sh"), ("windows", "ps1")):
        assert f"scripts/auth_init.{extension}" in manifest["hooks"]["preprovision"][platform]["run"]
        assert f"scripts/auth_update.{extension}" in manifest["hooks"]["postprovision"][platform]["run"]
