import json
import subprocess
import sys
from pathlib import Path

import check_release
from check_release import check_repository, inspect_file


def test_template_publication_check_detects_private_file_names_without_echoing_content():
    assert inspect_file(".env", b"secret local value")
    assert inspect_file(".azure/dev/.env", b"private")
    assert inspect_file("server.key", b"private")
    assert inspect_file(".env.example", b"AZURE_OPENAI_SERVICE=your-model-service") == []


def test_template_publication_check_recognizes_high_confidence_secrets():
    marker = b"-----BEGIN " + b"PRIVATE KEY-----"
    findings = inspect_file("example.txt", marker)
    assert findings and marker.decode() not in str(findings)
    assert inspect_file("example.txt", b"AccountKey=" + b"A" * 48)


def test_template_sample_profile_and_knowledge_are_checked():
    assert inspect_file("profile.json", json.dumps({"owner_name": "Alex Morgan"}).encode()) == []
    assert inspect_file("profile.json", b"not json")
    assert inspect_file("profile.json", json.dumps({"owner_name": "Someone Real"}).encode())
    assert inspect_file("data/work/private-notes.md", b"Potentially private source")
    assert inspect_file("data/work/profile.md", b"Fictional sample") == []


def test_publication_checks_distinguish_working_tree_from_index(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "profile.json"
    source.write_text(json.dumps({"owner_name": "Alex Morgan"}), encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text("PRIVATE_LOCAL_VALUE=not-for-publication", encoding="utf-8")
    subprocess.run(["git", "add", "profile.json", ".env"], cwd=tmp_path, check=True, capture_output=True)
    secret.unlink()
    assert check_repository(tmp_path) == []
    assert any(".env: private/generated" in error for error in check_repository(tmp_path, staged=True))
    source.write_text(json.dumps({"owner_name": "Changed Example"}), encoding="utf-8")
    assert any("fictional sample" in error for error in check_repository(tmp_path))
    assert not any("fictional sample" in error for error in check_repository(tmp_path, staged=True))


def test_publication_check_reports_missing_required_sample(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    assert check_repository(tmp_path) == ["profile.json: required fictional sample is absent from the selected tree"]


def test_publication_cli_returns_explicit_status_without_echoing_secrets(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_release.py", "--staged"])
    expected_root = Path(check_release.__file__).resolve().parents[1]

    def failures(root, staged):
        assert root == expected_root and staged
        return ["private.key: possible credential material"]

    monkeypatch.setattr(check_release, "check_repository", failures)
    assert check_release.main() == 1
    assert "No file contents or secrets were printed" in capsys.readouterr().out
    monkeypatch.setattr(check_release, "check_repository", lambda root, staged: [])
    assert check_release.main() == 0
    assert "checks passed" in capsys.readouterr().out
