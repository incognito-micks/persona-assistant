import runpy
from pathlib import Path


def test_small_container_uses_one_async_worker_and_allows_bounded_voice_drain():
    root = Path(__file__).resolve().parents[1]
    config = runpy.run_path(str(root / "app/backend/gunicorn.conf.py"))
    assert config["workers"] == 1
    assert config["worker_class"] == "custom_uvicorn_worker.CustomUvicornWorker"
    assert config["graceful_timeout"] >= 300
    assert config["control_socket_disable"] is True


def test_image_requires_prepared_profile_knowledge_and_frontend():
    dockerfile = (Path(__file__).resolve().parents[1] / "app/backend/Dockerfile").read_text()
    assert "load_knowledge_base(get_knowledge_base_path())" in dockerfile
    assert "load_profile()" in dockerfile
    assert "static/index.html" in dockerfile
    assert "TIKTOKEN_CACHE_DIR" in dockerfile
    assert "pip install gunicorn" not in dockerfile
