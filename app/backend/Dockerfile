FROM python:3.13-bookworm

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

ENV TIKTOKEN_CACHE_DIR=/app/tokenizer_cache
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"

COPY ./ /app
RUN python -c "from pathlib import Path; from core.knowledgebase import get_knowledge_base_path, load_knowledge_base; from core.profile import load_profile; load_knowledge_base(get_knowledge_base_path()); load_profile(); assert Path('static/index.html').is_file(), 'Build the frontend before packaging the image'"

CMD ["python3", "-m", "gunicorn", "-b", "0.0.0.0:8000", "main:app"]
