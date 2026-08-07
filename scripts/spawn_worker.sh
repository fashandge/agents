#!/bin/bash
# Lightweight worker spawn: a new tab, an agent, a task prompt, nothing else.
# No handoff protocol, no run directory, no registry, no monitoring. All
# argument parsing and terminal work occurs in Python; this shell boundary
# forwards argv literally and never evaluates user content.
set -euo pipefail

if [ -n "${SPAWN_PYTHON:-}" ]; then
  spawn_python=$SPAWN_PYTHON
elif [ -x /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python ]; then
  spawn_python=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
elif [ -x "${HOME:?}/miniforge3/envs/ml/bin/python" ]; then
  spawn_python="$HOME/miniforge3/envs/ml/bin/python"
else
  spawn_python=python3
fi

exec "$spawn_python" -m agents.orchestration.spawn_worker "$@"
