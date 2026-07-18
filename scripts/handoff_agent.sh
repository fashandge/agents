#!/bin/bash
# Safe local-v1 handoff launcher for local and SSH-hosted workers. All argument
# parsing, protocol initialization, and terminal-adapter work occurs in Python;
# this shell boundary forwards argv literally and never evaluates user content.
set -euo pipefail

if [ -n "${HANDOFF_PYTHON:-}" ]; then
  handoff_python=$HANDOFF_PYTHON
elif [ -x /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python ]; then
  handoff_python=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
elif [ -x "${HOME:?}/miniforge3/envs/ml/bin/python" ]; then
  handoff_python="$HOME/miniforge3/envs/ml/bin/python"
else
  handoff_python=python3
fi

exec "$handoff_python" -m agents.orchestration.handoff_launcher "$@"
