<!-- COMMON:BEGIN -->
# Project Engineering Rules

> Generated block. Source of truth: `agent-rules/AGENTS.base.md` in the
> soma_project workspace root. Edit the base file and run
> `node agent-rules/sync.mjs`; do not hand-edit this block.
>
> Read by Codex (`AGENTS.md`), Antigravity/agy (`AGENTS.md`), and Claude Code
> (via `@AGENTS.md` in `CLAUDE.md`).

## General

- Inspect the existing architecture before modifying code.
- Prefer minimal changes over unnecessary rewrites.
- Do not introduce dependencies unless they are clearly justified.
- Preserve existing APIs unless the task explicitly requires changing them.
- Never modify secrets, credentials, or production configuration.
- Do not commit `.env` files, keystores, or service-account JSON.
- Match the surrounding code: naming, structure, comment density, error handling.

## Development workflow

Before implementation:

1. Inspect the relevant files.
2. Identify the affected modules and their callers.
3. State your assumptions explicitly.
4. Check the existing tests that cover the area.

During implementation:

- Keep changes focused on the assigned task.
- Follow existing naming and architecture conventions.
- Avoid unrelated refactoring.

After implementation:

1. Run the relevant tests (see **Repo commands** below).
2. Run typecheck/lint if available.
3. Inspect `git diff`.
4. Report the files changed.
5. Report any unresolved concerns.

## Testing

A task is not complete merely because code was written. Completion requires at
least one of:

- passing automated tests
- a successful build
- a successful typecheck
- reproducible manual verification, with the steps written down

Never report success based on code you wrote but did not run. If a test was
already failing before your change, say so explicitly — do not claim it as your
own regression, and do not claim credit for fixing it.

## Git

- Do not force-push.
- Do not rewrite `main` / `master` / `dev` history.
- Do not delete branches unless explicitly requested.
- Keep commits focused: one logical change per commit.
- Do not commit unless the task explicitly asks you to.

## Multi-agent workflow

When operating as an Orca dispatched worker:

- The injected preamble is authoritative. Work only on the assigned task.
- Respect ownership boundaries. Do not modify files assigned to another worker.
- Use the preamble's `ask` command for a blocking question. Never open a local
  interactive prompt that the coordinator cannot answer.
- Send `worker_done` exactly once, from the dispatched terminal, with an
  explicit `--outcome succeeded` or `--outcome failed`. Never encode failure
  only in prose.
- Provide objective evidence for completion: the command you ran and its result.
- After `worker_done`, end the turn and idle. Do not start new work.

When two workers are deliberately given overlapping scope for comparison or
review, neither may edit the other's files. Each reports independently and the
coordinator decides.
<!-- COMMON:END -->

## Repo-specific — BE

FastAPI service (`team-EBD/BE`). Application code in `app/` (`api`, `core`,
`models`, `schemas`, `services`, plus `ai_client` / `push_client` /
`mail_client` / `social_client` / `billing_client` / `storage` adapters).
Entry point `app/main.py`. Schema migrations in `alembic/`. Tests in `tests/`.

### Repo commands

Use the checked-in virtualenv rather than the system interpreter:

| Purpose | Command (from repo root) |
| --- | --- |
| Tests | `venv/Scripts/python -m pytest` |
| One test file | `venv/Scripts/python -m pytest tests/test_auth.py` |
| Dev server | `venv/Scripts/python -m uvicorn app.main:app --reload` |
| Apply migrations | `venv/Scripts/alembic upgrade head` |
| New migration | `venv/Scripts/alembic revision --autogenerate -m "<summary>"` |

### Invariants

- The test suite needs **no external services**. `tests/conftest.py` runs the
  whole API against SQLite in-memory and replaces the social verifier, AI client
  and storage through `dependency_overrides`. If you find yourself needing a real
  database, AI key or network call to make a test pass, the test is wrong — fix
  the fixture, do not weaken the test.
- A model change without a matching Alembic migration is incomplete. Generate the
  migration and say in your report that one was added.
- Never point tests, scripts or defaults at production credentials or the
  production database. Never edit `.env`.
- API responses are consumed by the FE app already in the store. Do not rename or
  remove response fields without the task explicitly calling for it; add fields
  instead.
