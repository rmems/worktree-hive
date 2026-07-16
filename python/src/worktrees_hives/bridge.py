"""Thin subprocess bridge to the `wh` CLI binary.

The bridge locates ``wh`` via ``WH_BIN`` (environment variable) or ``PATH``,
invokes it with ``--json``, and parses the v1 JSON envelope on stdout.

**Layering:** Python never invokes ``git`` or ``gh`` directly for hive jobs, and
never reimplements Rust-owned safety, worktree, path, or branch checks. All
mutating and safety-sensitive operations go through ``wh`` (``git-safe``,
``gh-safe``, worktree, state, supervisor). This module is the only place Python
spawns ``wh``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from worktrees_hives.contract import (
    ErrorResponse,
    Response,
    SuccessResponse,
    classify,
)
from worktrees_hives.errors import (
    PolicyError,
    WhBinaryNotFoundError,
    WhJsonDecodeError,
    WhProcessError,
    WhSchemaError,
)


def _resolve_wh_binary(explicit_path: str | None = None) -> str:
    """Return the path to the wh binary.

    Resolution order:
    1. ``explicit_path`` argument (for testing or overrides).
    2. ``WH_BIN`` environment variable.
    3. ``wh`` on ``PATH``.

    Raises WhBinaryNotFoundError if none of these resolve.
    """
    if explicit_path is not None:
        if not os.path.isfile(explicit_path):
            raise WhBinaryNotFoundError(f"Explicit wh path does not exist: {explicit_path}")
        if not os.access(explicit_path, os.X_OK):
            raise WhBinaryNotFoundError(f"Explicit wh path is not executable: {explicit_path}")
        return explicit_path

    env_path = os.environ.get("WH_BIN")
    if env_path:
        if not os.path.isfile(env_path):
            raise WhBinaryNotFoundError(f"WH_BIN points to non-existent file: {env_path}")
        if not os.access(env_path, os.X_OK):
            raise WhBinaryNotFoundError(f"WH_BIN points to non-executable file: {env_path}")
        return env_path

    found = shutil.which("wh")
    if found is not None:
        return found

    raise WhBinaryNotFoundError()


class WhClient:
    """Subprocess client for the ``wh`` CLI.

    Parameters
    ----------
    wh_path:
        Explicit path to the ``wh`` binary.  If ``None``, the bridge resolves
        via ``WH_BIN`` or ``PATH``.
    timeout:
        Maximum seconds to wait for ``wh`` to complete.  ``None`` means no
        timeout (blocks indefinitely).
    """

    def __init__(
        self,
        wh_path: str | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self._wh_path = wh_path
        self._timeout = timeout

    def run(self, *args: str) -> SuccessResponse | ErrorResponse:
        """Invoke ``wh --json <args>`` and return a typed response envelope.

        ``git-safe`` / ``gh-safe`` map the *child* exit code onto the process
        exit while still writing an ``ok: true`` envelope with
        ``data.exit_code``. Machine clients must read that envelope rather than
        treating a non-zero process exit as a bridge failure.

        Raises
        ------
        WhBinaryNotFoundError
            If the ``wh`` binary cannot be located.
        WhProcessError
            If ``wh`` exits non-zero without a usable v1 envelope.
        PolicyError
            If ``wh`` exits 2 with a structured policy error envelope.
        WhJsonDecodeError
            If stdout is not valid JSON when an envelope was required.
        WhSchemaError
            If the decoded JSON does not match the v1 envelope.
        """
        binary = _resolve_wh_binary(self._wh_path)
        cmd = [binary, "--json", *args]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except OSError as exc:
            raise WhBinaryNotFoundError(f"Failed to execute wh at {binary}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise WhProcessError(
                returncode=-1,
                stderr=f"wh timed out after {self._timeout}s",
            ) from exc

        return self._interpret(result.returncode, result.stdout, result.stderr)

    def gh_safe(self, *args: str) -> SuccessResponse | ErrorResponse:
        """Run ``wh --json gh-safe <args>`` (Rust ``SafeGhCommand`` boundary)."""
        return self.run("gh-safe", *args)

    def git_safe(
        self, *args: str, expected_branch: str | None = None
    ) -> SuccessResponse | ErrorResponse:
        """Run ``wh --json git-safe …`` (Rust ``SafeGitCommand`` boundary)."""
        flags: list[str] = []
        if expected_branch is not None:
            flags.extend(["--expected-branch", expected_branch])
        return self.run("git-safe", *flags, *args)

    def bootstrap(self) -> SuccessResponse | ErrorResponse:
        """Run ``wh --json`` with no subcommand (bootstrap handshake)."""
        return self.run()

    def _interpret(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> SuccessResponse | ErrorResponse:
        """Map process exit + stdout to a typed envelope or raise."""
        stripped = stdout.strip()

        if stripped:
            try:
                classified = self._parse(stdout)
            except (WhJsonDecodeError, WhSchemaError):
                pass
            else:
                if isinstance(classified, ErrorResponse) and returncode == 2:
                    raise PolicyError(
                        classified.error.code,
                        classified.error.message,
                    )
                # Success envelope — including gh-safe/git-safe child failures
                # where process exit mirrors data.exit_code but ok is true.
                # ErrorResponse on non-2 exits is returned as structured data.
                return classified

        if returncode == 2:
            # Policy path without a usable envelope (same as #57).
            raise WhProcessError(
                returncode=returncode,
                stderr=stderr.strip(),
            )

        if returncode != 0:
            raise WhProcessError(
                returncode=returncode,
                stderr=stderr.strip(),
            )

        return self._parse(stdout)

    @staticmethod
    def _parse(raw_stdout: str) -> SuccessResponse | ErrorResponse:
        """Decode and validate the v1 JSON envelope from stdout."""
        stripped = raw_stdout.strip()
        if not stripped:
            raise WhJsonDecodeError(raw_stdout, cause=ValueError("empty output"))

        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise WhJsonDecodeError(stripped, cause=exc) from exc

        response = Response.from_dict(parsed)
        return classify(response)
