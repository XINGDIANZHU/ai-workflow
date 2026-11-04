#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export AI_WORKFLOW_HOME="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON_BIN:-$ROOT/.venv/bin/python}" -m hypercorn ai_workflow.runtime:app --bind "${AI_WORKFLOW_ENDPOINT_BIND:-127.0.0.1:29080}"
