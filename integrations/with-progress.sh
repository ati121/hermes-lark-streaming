#!/bin/sh
# Bind this directory read-only and use this wrapper as the Docker entrypoint.
# Reapply after image replacement; a future incompatible version still starts.
progress_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! python "$progress_dir/install_openviking_progress.py" --install; then
    printf '%s\n' '[hermes-progress] Extension unavailable; starting OpenViking without phase events.' >&2
fi
exec openviking-entrypoint "$@"
