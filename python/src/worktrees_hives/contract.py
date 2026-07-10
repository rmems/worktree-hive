"""Typed response envelope matching the Rust wh-core contract v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Schema version must match wh-core contract::SCHEMA_VERSION.
SCHEMA_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class ErrorData:
    """Structured error payload from a failed wh command."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Response:
    """The v1 JSON envelope returned by `wh --json`."""

    ok: bool
    schema_version: int
    command: str
    data: dict[str, Any]
    error: ErrorData | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Response:
        """Parse a raw dict (already JSON-decoded) into a Response.

        Raises WhSchemaError if required fields are missing or malformed.
        """
        from worktrees_hives.errors import WhSchemaError

        if not isinstance(raw, dict):
            raise WhSchemaError(f"Expected a JSON object, got {type(raw).__name__}")

        ok = raw.get("ok")
        if not isinstance(ok, bool):
            raise WhSchemaError(f"'ok' must be a bool, got {type(ok).__name__}")

        schema_version = raw.get("schema_version")
        if not isinstance(schema_version, int):
            raise WhSchemaError(
                f"'schema_version' must be an int, got {type(schema_version).__name__}"
            )

        command = raw.get("command")
        if not isinstance(command, str):
            raise WhSchemaError(
                f"'command' must be a str, got {type(command).__name__}"
            )

        data = raw.get("data")
        if not isinstance(data, dict):
            raise WhSchemaError(f"'data' must be a dict, got {type(data).__name__}")

        error_raw = raw.get("error")
        error: ErrorData | None = None
        if error_raw is not None:
            if not isinstance(error_raw, dict):
                raise WhSchemaError(
                    f"'error' must be a dict or null, got {type(error_raw).__name__}"
                )
            code = error_raw.get("code")
            message = error_raw.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise WhSchemaError(
                    "'error.code' and 'error.message' must be strings"
                )
            error = ErrorData(code=code, message=message)

        return cls(
            ok=ok,
            schema_version=schema_version,
            command=command,
            data=data,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class SuccessResponse:
    """Convenience wrapper for a successful Response."""

    command: str
    data: dict[str, Any]
    schema_version: int


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    """Convenience wrapper for a failed Response."""

    command: str
    error: ErrorData
    schema_version: int


def classify(response: Response) -> SuccessResponse | ErrorResponse:
    """Lift a Response into a typed Success or Error variant."""
    if response.ok:
        return SuccessResponse(
            command=response.command,
            data=response.data,
            schema_version=response.schema_version,
        )
    # error is guaranteed non-None when ok=False by the contract.
    return ErrorResponse(
        command=response.command,
        error=response.error or ErrorData(code="unknown", message="missing error"),
        schema_version=response.schema_version,
    )
