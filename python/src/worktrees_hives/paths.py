"""Platform-aware filesystem locations shared across the orchestrator.

Single source of truth for the worktree base directory. Both
:mod:`worktrees_hives.claim` and :mod:`worktrees_hives.issue_to_pr` derive
worktree paths from here; keeping two copies in sync is what let the macOS
branch go unexercised and break the macOS CI runner.

The layout mirrors the Rust side (``crates/wh-core/src/paths.rs``) so a
worktree created through ``wh`` and one resolved in Python land in the same
place.
"""

from __future__ import annotations

import os
import sys

WORKTREE_BASE_ENV = "WH_WORKTREE_BASE"

# Directory name used under every platform's user-data root.
_APP_DIR = "worktrees-hives"
_WORKTREE_DIR = "worktrees"


def default_worktree_base() -> str:
    """Return the default worktree base directory for this platform.

    Resolution order:

    1. ``WH_WORKTREE_BASE`` when set (explicit operator override).
    2. Windows: ``LOCALAPPDATA``, falling back to ``APPDATA``. The fallback is
       deliberate and matches Rust ``wh-core``.
    3. macOS: ``~/Library/Application Support``.
    4. ``XDG_DATA_HOME`` when set.
    5. ``~/.local/share``.
    """
    if override := os.environ.get(WORKTREE_BASE_ENV):
        return override
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if local:
            return os.path.join(local, _APP_DIR, _WORKTREE_DIR)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"),
            "Library",
            "Application Support",
            _APP_DIR,
            _WORKTREE_DIR,
        )
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, _APP_DIR, _WORKTREE_DIR)
    return os.path.join(
        os.path.expanduser("~"),
        ".local",
        "share",
        _APP_DIR,
        _WORKTREE_DIR,
    )
