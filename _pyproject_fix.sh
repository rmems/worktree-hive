#!/bin/bash
set -e
WORKTREE="/home/raulmc/.local/share/worktrees-hives/worktrees/rmems/worktrees-hives/python-package"
cd "$WORKTREE"
git add python/pyproject.toml
git commit -m "fix: add pytest to dev dependencies in pyproject.toml

The CI workflow installs the package with pip install -e './python[dev]'
but the dev extra was missing from pyproject.toml, causing pytest to not
be installed in the test environment."
COMMIT_SHA=$(git rev-parse HEAD)
echo "COMMIT_SHA=$COMMIT_SHA"
git push origin feature/python-package
echo "PUSHED=true"
