#!/bin/sh

set -e

. ./scripts/load_python_env.sh

exec ./.venv/bin/python ./scripts/auth_update.py
