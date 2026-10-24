#!/bin/sh

set -e

echo "Checking optional owner SPA setup; public visitors do not sign in."

. ./scripts/load_python_env.sh

exec ./.venv/bin/python ./scripts/auth_init.py
