#!/bin/sh
# Run the backend in the foreground; stop with Ctrl+C or your service manager.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/.local/restate-data"
exec "${RESTATE_BIN:-restate-server}" --no-logo --config-file "$ROOT/ops/restate.toml" --base-dir "$ROOT/.local/restate-data"
