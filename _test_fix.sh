#!/bin/bash
set -e
WORKTREE="/home/raulmc/.local/share/worktrees-hives/worktrees/rmems/worktrees-hives/python-package"
cd "$WORKTREE"
ruff format python/
git add python/
git commit -m "fix: set executable permission in tests for os.access(X_OK) check

The _resolve_wh_binary function now checks os.access(path, os.X_OK)
for explicit and WH_BIN paths. Tests that create mock wh binaries need
to set the executable bit (chmod 0o755) to pass this check."
COMMIT_SHA=$(git rev-parse HEAD)
echo "COMMIT_SHA=$COMMIT_SHA"
git push origin feature/python-package
echo "PUSHED=true"
