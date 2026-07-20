# Detached orchestrator watcher implementation kickoff

*(Historical record: this kickoff predates the coordinator→orchestrator
rename; "coordinator" below names the role now spelled "orchestrator" — see
`docs/plans/2026-07-19-orchestrator-rename-handoff.md`.)*

Implement the detached orchestrator watcher specified in `docs/2026-07-14_agent-handoff-communication-design.md`, especially section 4.6.1 and its acceptance tests. Read that design document fully first and treat it as authoritative.

Work in the existing `/Users/jianfuchen/projects/agents` checkout. It is intentionally dirty with related handoff protocol, registry, launcher, documentation, and test changes already in progress. Preserve and build on all pre-existing changes; do not stash, revert, overwrite, or commit unrelated user work. Do not commit or push.

Implementation requirements:

- Implement the coordinator-session detached watcher, coordinator ownership/registration, dynamic registry discovery, durable notification state, coalesced opaque orchestrator doorbells, restart recovery, and hot-path/recovery behavior described by DESIGN v1.4.
- Preserve the existing generic `handoffctl watch` behavior when coordinator state is not supplied.
- Do not implement stale-worker detection; it is explicitly out of scope.
- Keep the filesystem protocol authoritative. The watcher must remain credential-free and must never advance `control.outbox_cursor`, answer/review, or mutate worker inboxes.
- Use the repository's established import, environment, safety, and test conventions from `AGENTS.md`/`CLAUDE.md`.
- Add or update focused pytest coverage for the acceptance cases in section 7, including new-worker discovery without restart, coordinator isolation, cursor separation, coalescing, crash/restart recovery, exact opaque routing, and pending-state recovery.
- Update current architecture/agent documentation only where the implemented behavior changes it; keep implementation status truthful.

Verification:

```text
/opt/homebrew/Caskroom/miniconda/base/envs/ml/bin/python -m pytest tests/
```

Done when the feature is implemented in the current checkout, the relevant and full test suites pass, the working-tree diff is reviewed for unrelated damage, and a durable handoff `result` reports changed files, verification evidence, current HEAD, and dirty state.
