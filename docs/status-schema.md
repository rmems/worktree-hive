# Status JSON Schema

This document defines the JSON schema emitted by `wh status --json` and `wh jobs --json`. These commands allow Python orchestrators and agent platforms to query the state of watched worktree-hives jobs.

## Envelope

All status responses use the shared v1 envelope defined in the [JSON contract](json-contract.md):

| Field | Type | Description |
| --- | --- | --- |
| `ok` | `bool` | `true` when the query succeeded. |
| `schema_version` | `u8` | Always `1` for the current contract. |
| `command` | `string` | Machine-readable command identifier (`cli.status` or `cli.jobs`). |
| `data` | `object` | Command payload — contains a `jobs` array (see below). |
| `error` | `object \| null` | Structured error payload; always `null` on success. |

The `data` object for status commands has one field:

| Field | Type | Description |
| --- | --- | --- |
| `jobs` | `JobStatus[]` | Array of job status objects (may be empty). |

## `JobStatus` object

Each entry in the `jobs` array has these fields:

| Field | Type | Nullable | Description |
| --- | --- | --- | --- |
| `job_id` | `string` | no | Unique job identifier (e.g. `wh-347`). |
| `owner` | `string` | no | Repository owner (e.g. `rmems`). |
| `repo` | `string` | no | Repository name (e.g. `worktrees-hives`). |
| `issue_number` | `u64` | yes | Linked GitHub issue number. Omitted when absent. |
| `pr_number` | `u64` | yes | Linked pull-request number. Omitted when absent. |
| `worktree_path` | `string` | no | Absolute path to the job's isolated worktree. |
| `branch` | `string` | no | Current branch checked out in the worktree. |
| `process_state` | `ProcessState` | no | Lifecycle state of the job process. |
| `last_error` | `string` | yes | Last error message if the job failed. Omitted when absent. |
| `ci_class` | `CiClass` | no | CI classification for the job's head commit. |

## `ProcessState` enum

Serialized as a lowercase snake_case string.

| Variant | Meaning |
| --- | --- |
| `pending` | Job created but not yet started. |
| `running` | Job is actively running. |
| `completed` | Job finished successfully. |
| `failed` | Job terminated with an error. |
| `cancelled` | Job was cancelled by the operator. |

## `CiClass` enum

Serialized as a lowercase snake_case string.

| Variant | Meaning |
| --- | --- |
| `pass` | All required CI checks passed. |
| `fail` | At least one required check failed. |
| `pending` | Checks are queued or in progress. |
| `unknown` | CI status could not be determined. |

## Example: empty report

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "cli.status",
  "data": {
    "jobs": []
  },
  "error": null
}
```

## Example: report with jobs

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "cli.status",
  "data": {
    "jobs": [
      {
        "job_id": "wh-100",
        "owner": "rmems",
        "repo": "worktrees-hives",
        "issue_number": 29,
        "pr_number": 42,
        "worktree_path": "/home/user/.local/share/worktrees-hives/worktrees/rmems/worktrees-hives/wh-100",
        "branch": "feature/status-json-cli",
        "process_state": "running",
        "ci_class": "pending"
      },
      {
        "job_id": "wh-101",
        "owner": "rmems",
        "repo": "worktrees-hives",
        "issue_number": 30,
        "worktree_path": "/home/user/.local/share/worktrees-hives/worktrees/rmems/worktrees-hives/wh-101",
        "branch": "feature/python-bridge",
        "process_state": "failed",
        "last_error": "git command failed (`git push`): permission denied",
        "ci_class": "unknown"
      }
    ]
  },
  "error": null
}
```

## Versioning

The `schema_version` field is backward-compatible. Additive fields will be introduced within schema version `1`. Removals or semantic renames require a version bump. Consumers should ignore unknown fields.

## Commands

| Command | Description |
| --- | --- |
| `wh status --json` | Emit a status report for all watched jobs. |
| `wh jobs --json` | Alias for `wh status --json`. |

Without `--json`, both commands print a brief human-readable summary to standard output.

## Python consumption example

```python
import json
import subprocess

result = subprocess.run(
    ["wh", "status", "--json"],
    capture_output=True, text=True, check=True
)
report = json.loads(result.stdout)

assert report["schema_version"] == 1
assert report["error"] is None
for job in report["data"]["jobs"]:
    print(f"{job['job_id']}: {job['process_state']}")
```
