 #!/bin/sh

echo 'Creating Python virtual environment ".venv"...'
python3 -m venv .venv

echo 'Installing runtime and script dependencies into virtual environment (in quiet mode)...'
.venv/bin/python -m pip --quiet --disable-pip-version-check install -r scripts/requirements.txt
