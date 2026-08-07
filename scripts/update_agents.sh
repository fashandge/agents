#!/bin/bash
# update_agents.sh — update this agents checkout and the sibling skills
# checkout, then migrate persisted handoff state, in one operator-facing
# command.
#
# Usage:
#   update_agents.sh [--no-pull] [--dry-run] [--skills-dir <path>]
#   update_agents.sh --help
#
# Behavior:
#   * Default: git pull --ff-only in this checkout and in the skills checkout,
#     then run every cumulative state migration.
#   * --no-pull: skip both pulls and run only the migrations.
#   * --dry-run: never pull and pass --dry-run to every migration.
#
# Why skills too: agents ships the launcher and protocol, skills ships the
# instructions agents read (handoff-agent and friends, reached through the
# ~/.claude/skills and ~/.agents/skills symlinks). Syncing a box means both;
# updating only this repo leaves workers running new code against stale docs.
#
# Invariants:
#   * The skills checkout is optional. Absent, or present but not a git
#     worktree, is reported and skipped — never an error.
#   * A failed skills pull does not abort the migrations, which are the part
#     that must not be left half-done; it is reported in the summary and the
#     script exits non-zero afterward so it cannot pass unnoticed.
#   * Each migration is idempotent and takes its own backup before any write.
#   * HANDOFF_REGISTRY_FILE, when set, selects its parent as the state tree;
#     this keeps tests and non-default installations away from the default tree.
#   * The Python interpreter is resolved portably and printed before use.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: update_agents.sh [--no-pull] [--dry-run] [--skills-dir <path>]
       update_agents.sh --help

Pull this checkout and the skills checkout with git pull --ff-only, then run
the cumulative idempotent handoff state migrations. --no-pull migrates only.
--dry-run never pulls or writes state; it reports what each migration would
change.

The skills checkout defaults to $HOME/skills, overridden by --skills-dir or
HANDOFF_SKILLS_DIR. It is optional: a missing directory, or one that is not a
git worktree, is reported and skipped rather than failing the update.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

pull=true
dry_run=false
skills_dir=${HANDOFF_SKILLS_DIR:-$HOME/skills}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) pull=false; shift ;;
    --dry-run) pull=false; dry_run=true; shift ;;
    --skills-dir) skills_dir=${2:?'missing value for --skills-dir'}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

if [[ -n "${HANDOFF_PYTHON:-}" ]]; then
  PYTHON=$HANDOFF_PYTHON
elif [[ -x /opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python ]]; then
  PYTHON=/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python
elif [[ -x /home/opc/miniforge3/envs/ml/bin/python ]]; then
  PYTHON=/home/opc/miniforge3/envs/ml/bin/python
else
  PYTHON=$(command -v python3) || die "no Python interpreter found; set HANDOFF_PYTHON"
fi

if [[ "$PYTHON" == */* ]]; then
  [[ -x "$PYTHON" ]] || die "Python interpreter is not executable: $PYTHON"
else
  PYTHON=$(command -v "$PYTHON") || die "Python interpreter not found: $PYTHON"
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MIGRATION_ORCHESTRATOR=("$PYTHON" "$REPO_ROOT/scripts/migrate_handoff_state_orchestrator.py")
MIGRATION_JOURNALS=("$PYTHON" "$REPO_ROOT/scripts/migrate_handoff_state_journals.py")
if [[ -n "${HANDOFF_REGISTRY_FILE:-}" ]]; then
  [[ "$HANDOFF_REGISTRY_FILE" == /* ]] || die "HANDOFF_REGISTRY_FILE must be absolute"
  state_dir=$(dirname "$HANDOFF_REGISTRY_FILE")
  MIGRATION_ORCHESTRATOR+=(--state-dir "$state_dir")
  MIGRATION_JOURNALS+=(--state-dir "$state_dir")
fi
if [[ "$dry_run" == true ]]; then
  MIGRATION_ORCHESTRATOR+=(--dry-run)
  MIGRATION_JOURNALS+=(--dry-run)
fi

echo "python: $PYTHON"
if [[ "$pull" == true ]]; then
  echo "pull: running git pull --ff-only in $REPO_ROOT"
  git -C "$REPO_ROOT" pull --ff-only || die "git pull --ff-only failed"
  pull_result=completed
else
  echo "pull: skipped"
  pull_result=skipped
fi

# The skills checkout is commonly dirty with agent-managed files under
# .system/; --ff-only still succeeds when the incoming commits touch other
# paths, and fails loudly rather than merging when they collide.
if [[ "$pull" != true ]]; then
  echo "skills: skipped"
  skills_result=skipped
elif [[ ! -d "$skills_dir" ]]; then
  echo "skills: no checkout at $skills_dir; skipped"
  skills_result=absent
elif ! git -C "$skills_dir" rev-parse --git-dir >/dev/null 2>&1; then
  echo "skills: $skills_dir is not a git worktree; skipped"
  skills_result=absent
else
  echo "skills: running git pull --ff-only in $skills_dir"
  if git -C "$skills_dir" pull --ff-only; then
    skills_result=completed
  else
    echo "ERROR: skills pull failed in $skills_dir; agent instructions may be stale" >&2
    skills_result=failed
  fi
fi

echo "migration: running ${MIGRATION_ORCHESTRATOR[*]}"
"${MIGRATION_ORCHESTRATOR[@]}" || die "orchestrator rename migration failed"
echo "migration: running ${MIGRATION_JOURNALS[*]}"
"${MIGRATION_JOURNALS[@]}" || die "journal-state migration failed"
if [[ "$dry_run" == true ]]; then
  migration_result=dry-run
else
  migration_result=completed
fi
echo "update complete: pull=$pull_result skills=$skills_result migration=$migration_result"
# Deferred so the migrations still run and report; a stale skills checkout is a
# real partial failure and must not exit 0.
[[ "$skills_result" == failed ]] && exit 1
exit 0
