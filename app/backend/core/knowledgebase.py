import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def get_knowledge_base_path(configured_path: Optional[str] = None) -> str:
    """Resolve required context in a checkout or a backend-only deployment."""
    backend_dir = Path(__file__).resolve().parent.parent
    repository_dir = backend_dir.parent.parent
    base_dir = repository_dir if (repository_dir / "azure.yaml").is_file() else backend_dir
    if configured_path is not None:
        if not configured_path.strip():
            raise ValueError("KNOWLEDGE_DATA_DIR must not be empty")
        path = Path(configured_path).expanduser()
        return str(path if path.is_absolute() else (base_dir / path).resolve())

    return str(base_dir / "data" / "work")


def load_knowledge_base(data_dir: str) -> list[str]:
    """Load required markdown context or raise when it cannot be loaded.

    Returns a list of formatted source strings, one per file,
    ready to inject into the LLM prompt as text_sources.
    """
    sources: list[str] = []
    data_path = Path(data_dir)

    if not data_path.exists():
        raise FileNotFoundError(f"Required knowledge base directory not found: {data_path}")
    if not data_path.is_dir():
        raise NotADirectoryError(f"Required knowledge base path is not a directory: {data_path}")

    md_files = sorted(data_path.glob("*.md"))
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                sources.append(f"[{md_file.stem}]: {content}")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"Unable to read required knowledge file: {md_file}") from error

    if not sources:
        raise ValueError(f"Required knowledge base contains no nonempty markdown files: {data_path}")

    logger.info(
        "Loaded %d knowledge base files from %s (total %d chars)", len(sources), data_dir, sum(len(s) for s in sources)
    )
    return sources
