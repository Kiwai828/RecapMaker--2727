#!/usr/bin/env sh
set -eu

# Cloudflare Workers Builds documents Python, pip, pipx and curl, but does not
# list uv/uvx as a preinstalled tool. Prefer an existing uvx and install uv
# only when the build image does not provide it.
if ! command -v uvx >/dev/null 2>&1; then
  export UV_NO_MODIFY_PATH=1
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

cd cloudflare
uvx --from workers-py pywrangler sync

# Fail before deployment if the Workers Python package tree was not generated.
# Without this check, Wrangler can upload the entrypoint while Pyodide later
# raises ModuleNotFoundError for FastAPI during validation.
if [ ! -f python_modules/fastapi/__init__.py ]; then
  echo "Build error: python_modules/fastapi was not generated; run pywrangler sync." >&2
  exit 1
fi
