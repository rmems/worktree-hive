# JSON Contract v1

This document describes the versioned JSON envelope used for communication between the Python orchestrator and the Rust `wh` CLI.

## Envelope Structure

All `--json` responses follow this schema:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "command.name",
  "data": {},
  "error": null
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `ok` | `boolean` | `true` for success, `false` for failure |
| `schema_version` | `integer` | Always `1` for this version |
| `command` | `string` | Machine-readable command identifier (e.g., `worktree.create`, `state.add`) |
| `data` | `object` | Command-specific payload (empty object `{}` on success for commands without output) |
| `error` | `object \| null` | Present only when `ok: false` |

### Error Object

```json
{
  "code": "ERROR_CODE",
  "message": "Human-readable description"
}
```

Standard error codes:
- `PolicyMergeForbidden` — Attempted to merge a PR (never allowed)
- `PolicyForcePushForbidden` — Bare `--force`/`-f` used (only `--force-with-lease` allowed)
- `PolicyBranchMismatch` — Current branch doesn't match expected job branch
- `PolicyPathEscape` — Path traversal outside sandbox
- `PolicyGitSubcommandNotAllowed` — Git subcommand not in allowlist
- `PolicyGhSubcommandNotAllowed` — GH subcommand not in allowlist
- `PolicyGhApiDenied` — `gh api` denied by default in v1
- `WhBinaryNotFoundError` — `wh` binary not on PATH or `WH_BIN`
- `WhProcessError` — `wh` exited with non-zero status
- `WhJsonDecodeError` — Stdout was not valid JSON
- `WhSchemaError` — JSON did not match v1 envelope

## Commands

### Bootstrap

```bash
wh --json
```

**Response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "cli.bootstrap",
  "data": {},
  "error": null
}
```

### Worktree Operations

#### `worktree.create`

Create a new isolated worktree for a job.

```bash
wh --json worktree create --repo /path/to/repo acme example-repo wh-123 feature/fix
```

**Request parameters:** `--repo` flag plus positionals `<owner> <repo_name> <job_id> <branch>`.

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.create",
  "data": {
    "path": "/home/user/.local/share/worktrees-hives/worktrees/acme/example-repo/wh-123",
    "branch": "feature/fix",
    "repo_root": "/path/to/repo"
  },
  "error": null
}
```

#### `worktree.list`

List all hive worktrees.

```bash
wh --json worktree list
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.list",
  "data": {
    "worktrees": [
      { "path": "/.../wh-123", "branch": "feature/fix" },
      { "path": "/.../wh-124", "branch": "feature/other" }
    ]
  },
  "error": null
}
```

#### `worktree.remove`

Remove a worktree.

```bash
wh --json worktree remove /path/to/worktree --force
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.remove",
  "data": { "removed": "/path/to/worktree" },
  "error": null
}
```

#### `worktree.prune`

Prune stale worktree administrative files.

```bash
wh --json worktree prune --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "worktree.prune",
  "data": {},
  "error": null
}
```

### State Operations

#### `state.show`

Show watched jobs.

```bash
# Show all
wh --json state show

# Show specific job
wh --json state show acme example-repo 42
```

**Success response (all):**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.show",
  "data": {
    "jobs": [
      {
        "owner": "acme",
        "repo": "example-repo",
        "number": 42,
        "kind": "issue",
        "branch": "issue-42-fix",
        "worktree_path": "/home/user/.local/share/worktrees-hives/worktrees/acme/example-repo/wh-42",
        "stack_id": "stack-1",
        "status": "babysitting",
        "fix_count": 1,
        "residual_blockers": [],
        "created_at": 1700000000,
        "updated_at": 1700000100
      }
    ]
  },
  "error": null
}
```

#### `state.add`

Add a new watched job.

```bash
wh --json state add acme example-repo 42 --kind issue --branch issue-42-fix --worktree /path/to/wt --stack stack-1
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.add",
  "data": {
    "owner": "acme",
    "repo": "example-repo",
    "number": 42,
    "kind": "issue",
    "branch": "issue-42-fix",
    "worktree_path": "/path/to/wt",
    "stack_id": "stack-1",
    "status": "claimed",
    "fix_count": 0,
    "residual_blockers": [],
    "created_at": 1700000000,
    "updated_at": 1700000000
  },
  "error": null
}
```

#### `state.remove`

Remove a watched job.

```bash
wh --json state remove acme example-repo 42
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "state.remove",
  "data": { "removed": true },
  "error": null
}
```

### Git Operations

#### `git.run`

Run an allowlisted git command.

```bash
wh --json git run status --porcelain
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "git.run",
  "data": {
    "success": true,
    "stdout": "M  src/main.rs\n",
    "stderr": ""
  },
  "error": null
}
```

#### `git.verify-branch`

Verify the current branch matches the expected branch.

```bash
wh --json git verify-branch feature/fix --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "git.verify-branch",
  "data": { "matched": true },
  "error": null
}
```

### GH Operations

#### `gh.run`

Run an allowlisted gh command.

```bash
wh --json gh run pr view 42 --json number,title,state
```

#### `gh.pr-view`

View a PR with specific fields.

```bash
wh --json gh pr-view 42 --fields number,title,state --repo /path/to/repo
```

**Success response:**
```json
{
  "ok": true,
  "schema_version": 1,
  "command": "gh.pr-view",
  "data": {
    "success": true,
    "stdout": "{\"number\":42,\"title\":\"Fix bug\",\"state\":\"OPEN\"}",
    "stderr": ""
  },
  "error": null
}
```

## Compatibility Policy

- **Additive changes** (new optional fields in `data`, new commands) are compatible within v1.
- **Breaking changes** (removing/renaming fields, changing types, removing commands) require a schema version bump to v2.
- Consumers MUST ignore unknown fields in `data`.
- Consumers MUST handle `error` being `null` or an object.

## Fixtures

Example JSON files for testing are located in `docs/examples/`:

- `bootstrap.json`
- `worktree-create.json`
- `worktree-list.json`
- `state-show.json`
- `state-add.json`
- `git-run.json`
- `error-policy.json`

## Python Validation

The Python `worktrees_hives.contract` module provides `Response.from_dict()` for validation. It raises `WhSchemaError` if the envelope doesn't match v1.

```python
from worktrees_hives.contract import Response, classify
from worktrees_hives.errors import WhSchemaError

raw = json.loads(stdout)
response = Response.from_dict(raw)  # Validates schema
typed = classify(response)  # SuccessResponse | ErrorResponse
```

## Rust Validation

The Rust `wh_core::contract` module provides the same validation via `Response::from_dict()`.