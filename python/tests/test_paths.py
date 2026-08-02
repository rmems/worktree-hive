"""Tests for worktrees_hives.paths.

Every platform branch is exercised on every CI runner by patching
``sys.platform`` rather than branching on the runner's own platform. The
previous arrangement (a runner-conditional assertion in test_issue_to_pr.py)
left the macOS branch untested everywhere except macOS, which is how it
reached CI broken.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from worktrees_hives.paths import WORKTREE_BASE_ENV, default_worktree_base

# Env vars that steer resolution; cleared before each case so the branch under
# test is the only thing deciding the result.
_STEERING_ENV = (
    WORKTREE_BASE_ENV,
    "XDG_DATA_HOME",
    "LOCALAPPDATA",
    "APPDATA",
)

_TAIL = ("worktrees-hives", "worktrees")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Clear steering vars and pin the home directory to a temp dir."""
    for name in _STEERING_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    # HOME covers POSIX expanduser; USERPROFILE covers a real Windows runner.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_env_override_wins_on_every_platform(monkeypatch, clean_env):
    """WH_WORKTREE_BASE short-circuits before any platform logic."""
    monkeypatch.setenv(WORKTREE_BASE_ENV, "/srv/custom-worktrees")
    for platform in ("win32", "darwin", "linux"):
        monkeypatch.setattr("sys.platform", platform)
        assert default_worktree_base() == "/srv/custom-worktrees"


def test_win32_uses_localappdata(monkeypatch, clean_env):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", os.path.join("C:", "Users", "u", "AppData", "Local"))
    assert Path(default_worktree_base()).parts[-2:] == _TAIL
    assert "Local" in Path(default_worktree_base()).parts


def test_win32_falls_back_to_appdata(monkeypatch, clean_env):
    """LOCALAPPDATA unset falls back to APPDATA — deliberate, matches Rust wh-core."""
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", os.path.join("C:", "Users", "u", "AppData", "Roaming"))
    parts = Path(default_worktree_base()).parts
    assert parts[-2:] == _TAIL
    assert "Roaming" in parts


def test_win32_without_appdata_falls_through_to_home(monkeypatch, clean_env):
    """Neither var set: fall through rather than emit a path rooted at None."""
    monkeypatch.setattr("sys.platform", "win32")
    result = Path(default_worktree_base())
    assert result.parts[-2:] == _TAIL
    assert result.is_absolute()


def test_darwin_uses_application_support(monkeypatch, clean_env):
    """The branch that broke the macOS runner — now exercised everywhere."""
    monkeypatch.setattr("sys.platform", "darwin")
    parts = Path(default_worktree_base()).parts
    assert parts[-4:] == ("Library", "Application Support", *_TAIL)


def test_darwin_ignores_xdg_data_home(monkeypatch, clean_env):
    """macOS prefers Application Support even when XDG_DATA_HOME is set."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("XDG_DATA_HOME", "/should/not/be/used")
    assert "Application Support" in default_worktree_base()
    assert "should/not/be/used" not in default_worktree_base()


def test_linux_prefers_xdg_data_home(monkeypatch, clean_env, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert Path(default_worktree_base()) == xdg / "worktrees-hives" / "worktrees"


def test_linux_falls_back_to_local_share(monkeypatch, clean_env):
    monkeypatch.setattr("sys.platform", "linux")
    expected = clean_env / ".local" / "share" / "worktrees-hives" / "worktrees"
    assert Path(default_worktree_base()) == expected


def test_result_is_always_absolute(monkeypatch, clean_env):
    """No branch may return a relative path — worktree sandboxing depends on it."""
    for platform in ("win32", "darwin", "linux", "freebsd"):
        monkeypatch.setattr("sys.platform", platform)
        assert Path(default_worktree_base()).is_absolute(), platform
