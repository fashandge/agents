#!/bin/bash
# cmux_close_surface_safe.sh — the only sanctioned way to close a cmux viewer
# tab. Closes exactly one surface by UUID, with snapshot/verify guards around
# the mutation (the handoff-agent skill's layout-safety procedure).
#
# Usage:
#   cmux_close_surface_safe.sh --surface <uuid> [--workspace <id>]
#     --workspace defaults to $CMUX_WORKSPACE_ID.
#
# Guard procedure:
#   1. Snapshot `cmux --id-format both list-pane-surfaces --workspace <id>`
#      and `cmux --id-format both list-workspaces`.
#   2. Verify the target surface exists in the snapshot; if not, error out —
#      nothing is mutated.
#   3. `cmux close-surface --surface <uuid>`.
#   4. Re-list and verify every OTHER pre-existing surface and workspace is
#      still present. If anything else disappeared, exit nonzero with a loud
#      message telling the operator to STOP all layout actions and report to
#      the user.
#
# Invariants:
#   * This script never calls close-window, close-workspace, or any tab-action
#     sweep (close-others / close-left / close-right) — those can destroy
#     unrelated user sessions and the orchestrator's own surface.
#   * Every cleanup action names the exact UUID of the one thing it removes —
#     nothing positional, nothing by name pattern, never a sweep.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: cmux_close_surface_safe.sh --surface <uuid> [--workspace <id>]

Close exactly one cmux surface by UUID (default workspace: $CMUX_WORKSPACE_ID),
verifying before and after that no other surface or workspace disappeared.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

UUID_RE='[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
cmux_bin=${CMUX_BIN:-cmux}

surface=""
workspace=${CMUX_WORKSPACE_ID:-}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)   surface=${2:?'missing value for --surface'}; shift 2 ;;
    --workspace) workspace=${2:?'missing value for --workspace'}; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; die "unknown argument: $1" ;;
  esac
done

[[ -n "$surface" ]] || { usage >&2; die "--surface is required"; }
[[ -n "$workspace" ]] || die "--workspace is required (CMUX_WORKSPACE_ID is unset)"
[[ "$surface" =~ ^${UUID_RE}$ ]] || die "--surface must be a full UUID, got: $surface"
target=$(tr 'A-F' 'a-f' <<<"$surface")

# All UUIDs mentioned anywhere in a listing, lowercased and deduplicated.
uuids() { grep -oiE "$UUID_RE" | tr 'A-F' 'a-f' | sort -u; }

before_surfaces=$("$cmux_bin" --id-format both list-pane-surfaces --workspace "$workspace")
before_workspaces=$("$cmux_bin" --id-format both list-workspaces)
surface_uuids_before=$(uuids <<<"$before_surfaces")
workspace_uuids_before=$(uuids <<<"$before_workspaces")

grep -qxF "$target" <<<"$surface_uuids_before" \
  || die "surface $surface not found in workspace $workspace — nothing mutated"

# UUIDs sharing a listing line with the target (its own pane/tab entries)
# legitimately vanish with it; exclude them from the disappearance check.
target_line_uuids=$(grep -i "$target" <<<"$before_surfaces" | uuids)

"$cmux_bin" close-surface --surface "$surface"
echo "closed surface $surface"
sleep 1

after_surfaces=$("$cmux_bin" --id-format both list-pane-surfaces --workspace "$workspace")
after_workspaces=$("$cmux_bin" --id-format both list-workspaces)
surface_uuids_after=$(uuids <<<"$after_surfaces")
workspace_uuids_after=$(uuids <<<"$after_workspaces")

missing=0
while read -r u; do
  [[ -n "$u" ]] || continue
  [[ "$u" == "$target" ]] && continue
  grep -qxF "$u" <<<"$target_line_uuids" && continue
  # Workspace UUIDs embedded in the surface listing are checked separately.
  grep -qxF "$u" <<<"$workspace_uuids_before" && continue
  grep -qxF "$u" <<<"$surface_uuids_after" && continue
  echo "ERROR: pre-existing surface $u disappeared after closing $surface" >&2
  missing=1
done <<<"$surface_uuids_before"

while read -r u; do
  [[ -n "$u" ]] || continue
  grep -qxF "$u" <<<"$workspace_uuids_after" && continue
  echo "ERROR: pre-existing workspace $u disappeared after closing $surface" >&2
  missing=1
done <<<"$workspace_uuids_before"

if [[ "$missing" -ne 0 ]]; then
  echo "ERROR: layout changed beyond the target surface — STOP all further layout actions immediately and report this to the user." >&2
  exit 1
fi
echo "verify: every other pre-existing surface and workspace is still present"
