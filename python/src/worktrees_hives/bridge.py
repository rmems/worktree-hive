"""Thin subprocess bridge to the `wh` CLI binary.

The bridge locates ``wh`` via ``WH_BIN`` (environment variable) or ``PATH``,
invokes it with ``--json``, and parses the v1 JSON envelope on stdout.

Python never duplicates Rust-owned safety checks — it delegates to ``wh`` and
interprets the structured response.
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

        Raises
        ------
        WhBinaryNotFoundError
            If the ``wh`` binary cannot be located.
        WhProcessError
            If ``wh`` exits with a non-zero status code.
        WhJsonDecodeError
            If stdout is not valid JSON.
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

        if result.returncode != 0:
            # Exit code 2 = policy violation — validate v1 envelope before PolicyError
            if result.returncode == 2 and result.stdout.strip():
                try:
                    parsed = json.loads(result.stdout.strip())
                except json.JSONDecodeError:
                    pass
                else:
                    response = Response.from_dict(parsed)
                    classified = classify(response)
                    if isinstance(classified, ErrorResponse):
                        raise PolicyError(
                            classified.error.code,
                            classified.error.message,
                        )

            raise WhProcessError(
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )

        return self._parse(result.stdout)

    def bootstrap(self) -> SuccessResponse | ErrorResponse:
        """Run ``wh --json`` with no subcommand (bootstrap handshake)."""
        return self.run()

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
