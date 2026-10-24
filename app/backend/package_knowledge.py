import os
from pathlib import Path
from shutil import copyfile

from core.knowledgebase import get_knowledge_base_path, load_knowledge_base
from core.profile import get_profile_path, load_profile


def package_knowledge_base(source_dir: Path, destination_dir: Path) -> int:
    """Validate and refresh the markdown bundled with a backend deployment."""
    sources = load_knowledge_base(str(source_dir))
    source_files = sorted(source_dir.glob("*.md"))
    source_names = {source_file.name for source_file in source_files}
    destination_dir.mkdir(parents=True, exist_ok=True)

    for previous_file in destination_dir.glob("*.md"):
        if previous_file.name not in source_names:
            previous_file.unlink()

    for source_file in source_files:
        copyfile(source_file, destination_dir / source_file.name)

    return len(sources)


if __name__ == "__main__":
    backend_dir = Path(__file__).resolve().parent
    source_dir = Path(get_knowledge_base_path(os.getenv("KNOWLEDGE_DATA_DIR")))
    profile_path = get_profile_path(os.getenv("PROFILE_PATH"))
    load_profile(str(profile_path))
    destination_dir = backend_dir / "data" / "work"
    source_count = package_knowledge_base(source_dir, destination_dir)
    copyfile(profile_path, backend_dir / "profile.json")
    print(f"Packaged {source_count} knowledge sources in {destination_dir}")
