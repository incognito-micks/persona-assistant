import argparse
import json
import re
import subprocess
from pathlib import Path

PRIVATE_FILE_SUFFIXES = {".pem", ".key", ".pfx", ".p12"}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}\b"),
    re.compile(rb"(?:AccountKey|SharedAccessKey)\s*=\s*[A-Za-z0-9+/]{40,}={0,2}", re.IGNORECASE),
]
SAMPLE_KNOWLEDGE_FILES = {"data/work/profile.md", "data/work/projects.md"}


def inspect_file(name: str, content: bytes) -> list[str]:
    path = Path(name)
    errors = []
    if (
        path.suffix.lower() in PRIVATE_FILE_SUFFIXES
        or any(part in {".azure", ".venv", "node_modules", "private"} for part in path.parts)
        or (path.name.startswith(".env") and path.name != ".env.example")
    ):
        errors.append("private/generated file must not be published")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        errors.append("possible credential/private-key material; inspect locally")
    if name.startswith("data/work/") and name not in SAMPLE_KNOWLEDGE_FILES:
        errors.append("unexpected source in the template sample; review publication rights")
    if name == "profile.json":
        try:
            profile = json.loads(content)
        except (ValueError, UnicodeError):
            errors.append("invalid sample profile")
        else:
            if not isinstance(profile, dict) or profile.get("owner_name") != "Alex Morgan":
                errors.append("template release must use its fictional sample profile")
    return errors


def check_repository(root: Path, staged: bool = False) -> list[str]:
    args = (
        ["git", "ls-files", "-z"] if staged else ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    files = subprocess.run(args, cwd=root, capture_output=True, check=True).stdout.decode().split("\0")
    failures: list[str] = []
    seen = set()
    for name in files:
        if not name or name in seen:
            continue
        seen.add(name)
        if staged:
            content = subprocess.run(["git", "show", f":{name}"], cwd=root, capture_output=True, check=True).stdout
        else:
            path = root / name
            if not path.is_file():
                continue
            content = path.read_bytes()
        failures.extend(f"{name}: {message}" for message in inspect_file(name, content))
    if "profile.json" not in seen:
        failures.append("profile.json: required fictional sample is absent from the selected tree")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local template-publication checks; never uploads source or credentials."
    )
    parser.add_argument("--staged", action="store_true", help="Inspect the index rather than working-tree content.")
    args = parser.parse_args()
    failures = check_repository(Path(__file__).resolve().parents[1], args.staged)
    for failure in failures:
        print(failure)
    if failures:
        print(f"Publication checks failed for {len(failures)} item(s). No file contents or secrets were printed.")
        return 1
    print(
        "Template checks passed. Still review history, media/content rights, and provider/repository settings manually."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
