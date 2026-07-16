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
    PolicyError,
    WhBinaryNotFoundError,
    WhError,
    WhJsonDecodeError,
    WhProcessError,
    WhSchemaError,
)
from worktrees_hives.orchestrator import (
    Orchestrator,
    OrchestratorReport,
    WorkerResult,
    WorkerSpec,
    WorkerStatus,
)

__version__ = "0.1.0"

__version__ = "0.1.0"

__all__ = [
    "AttributionConfig",
    "AttributionPlacement",
    "ErrorResponse",
    "Orchestrator",
    "OrchestratorReport",
    "PolicyError",
    "ReplyTemplate",
    "Response",
    "SuccessResponse",
    "WhBinaryNotFoundError",
    "WhClient",
    "WhError",
    "WhJsonDecodeError",
    "WhProcessError",
    "WhSchemaError",
    "WorkerResult",
    "WorkerSpec",
    "WorkerStatus",
    "__version__",
    "format_attribution",
    "format_reply",
]
