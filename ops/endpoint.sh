#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export AI_WORKFLOW_HOME="$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON_BIN:-$ROOT/.venv/bin/python}" -m ai_workflow.server
