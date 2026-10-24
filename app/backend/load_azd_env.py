import json
import logging
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("scripts")


def load_azd_env():
    """Prefer explicit local configuration, otherwise use the selected azd environment."""
    backend_dir = Path(__file__).resolve().parent
    repository_dir = backend_dir.parent.parent
    environment_dir = repository_dir if (repository_dir / "azure.yaml").is_file() else backend_dir
    local_env = environment_dir / ".env"
    if local_env.is_file():
        load_dotenv(local_env, override=False)
    if os.getenv("AZURE_OPENAI_SERVICE") or os.getenv("AZURE_OPENAI_CUSTOM_URL"):
        return
    try:
        result = subprocess.run(["azd", "env", "list", "-o", "json"], capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise RuntimeError(
            "Configure the root .env from .env.example, or install azd and select an environment."
        ) from error
    if result.returncode != 0:
        raise RuntimeError("Configure the root .env from .env.example, or create/select an azd environment.")
    env_json = json.loads(result.stdout)
    env_file_path = None
    for entry in env_json:
        if entry["IsDefault"]:
            env_file_path = entry["DotEnvPath"]
    if not env_file_path:
        raise RuntimeError("No selected azd environment. Configure the root .env or select an azd environment.")
    loading_mode = os.getenv("LOADING_MODE_FOR_AZD_ENV_VARS") or "override"
    if loading_mode == "no-override":
        logger.info("Loading azd env from %s, but not overriding existing environment variables", env_file_path)
        load_dotenv(env_file_path, override=False)
    else:
        logger.info("Loading azd env from %s, which may override existing environment variables", env_file_path)
        load_dotenv(env_file_path, override=True)
