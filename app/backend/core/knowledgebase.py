import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
ELAPSED_TOKEN = re.compile(r"\[\[elapsed:([^\]]*)\]\]")


def render_knowledge_sources(sources: list[str], as_of: Optional[date] = None) -> list[str]:
    """Resolve trusted date tokens per request without changing the cached sources."""
    today = as_of if as_of is not None else datetime.now(UTC).date()

    def elapsed(match: re.Match[str]) -> str:
        value = match.group(1)
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
            raise ValueError("Knowledge elapsed tokens must use YYYY-MM")
        try:
            start = date.fromisoformat(f"{value}-01")
        except ValueError as error:
            raise ValueError("Knowledge elapsed tokens must contain a valid year and month") from error
        months = (today.year - start.year) * 12 + today.month - start.month
        if months < 0:
            raise ValueError("Knowledge elapsed start month must not be in the future")
        years, remaining_months = divmod(months, 12)
        parts = []
        if years:
            parts.append(f"{years} year" + ("s" if years != 1 else ""))
        if remaining_months:
            parts.append(f"{remaining_months} month" + ("s" if remaining_months != 1 else ""))
        return " and ".join(parts) if parts else "less than one month"

    rendered = []
    for source in sources:
        value = ELAPSED_TOKEN.sub(elapsed, source.replace("[[today]]", today.isoformat()))
        if "[[elapsed:" in value:
            raise ValueError("Knowledge elapsed token is not closed")
        rendered.append(value)
    return rendered


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

    # Validate date tokens now, but retain them so later requests do not freeze the date.
    render_knowledge_sources(sources)
    logger.info(
        "Loaded %d knowledge base files from %s (total %d chars)", len(sources), data_dir, sum(len(s) for s in sources)
    )
    return sources
