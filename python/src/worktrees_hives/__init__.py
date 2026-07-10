"""worktrees-hives: Python orchestrator for issue-to-PR and PR-babysit workflows."""

__version__ = "0.1.0"

from worktrees_hives.attribution import (
    AttributionConfig,
    AttributionPlacement,
    ReplyTemplate,
    format_attribution,
    format_reply,
)
from worktrees_hives.bridge import WhClient
from worktrees_hives.contract import ErrorResponse, Response, SuccessResponse
from worktrees_hives.errors import (
    WhBinaryNotFoundError,
    WhError,
    WhJsonDecodeError,
    WhProcessError,
    WhSchemaError,
)

__all__ = [
    "AttributionConfig",
    "AttributionPlacement",
    "ErrorResponse",
    "ReplyTemplate",
    "Response",
    "SuccessResponse",
    "WhBinaryNotFoundError",
    "WhClient",
    "WhError",
    "WhJsonDecodeError",
    "WhProcessError",
    "WhSchemaError",
    "format_attribution",
    "format_reply",
]
