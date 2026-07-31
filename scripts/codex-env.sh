#!/bin/bash
# Source this script to get CODEX_NODE_BIN and CODEX_BIN environment variables.
# Automatically finds the newest NVM node version that has codex installed.
#
# Usage:
#   source ~/projects/agents/codex-env.sh
#   "${CODEX_NODE_BIN}" "${CODEX_BIN}" exec --model gpt-5.6-luna ...

_codex_env_nvm_dir="$HOME/.nvm/versions/node"

if [[ ! -d "$_codex_env_nvm_dir" ]]; then
  echo "Error: NVM directory not found: $_codex_env_nvm_dir" >&2
  return 1 2>/dev/null || exit 1
fi

# Search versions newest-first using glob + sort (avoids ls color code issues)
for _codex_env_version_dir in $(printf '%s\n' "$_codex_env_nvm_dir"/v* | sort -rV); do
  _codex_env_codex_path="$_codex_env_version_dir/bin/codex"
  _codex_env_node_path="$_codex_env_version_dir/bin/node"

  if [[ -e "$_codex_env_codex_path" && -x "$_codex_env_node_path" ]]; then
    export CODEX_NODE_BIN="$_codex_env_node_path"
    export CODEX_BIN="$_codex_env_codex_path"
    break
  fi
done

# Cleanup temp variables
unset _codex_env_nvm_dir _codex_env_version_dir _codex_env_codex_path _codex_env_node_path

if [[ -z "$CODEX_BIN" ]]; then
  echo "Error: codex not found in any NVM node version" >&2
  return 1 2>/dev/null || exit 1
fi
