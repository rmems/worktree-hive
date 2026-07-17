"""worktrees-hives: Python orchestrator for issue-to-PR and PR-babysit workflows.

Orchestration policy only. Worktrees, path sandbox, branch checks, and safe
``git``/``gh`` execution belong to the Rust ``wh`` binary — use :class:`WhClient`.
"""

__version__ = "0.1.0"

from worktrees_hives.attribution import (
    AttributionConfig,
    AttributionPlacement,
    ReplyTemplate,
    format_attribution,
    format_reply,
)
from worktrees_hives.babysit import (
    BabysitCycle,
    BabysitResult,
    CheckRun,
    PRState,
    ReviewThread,
    ThreadAction,
    babysit_multiple,
    classify_pr,
)
from worktrees_hives.bridge import WhClient
from worktrees_hives.ci_taxonomy import (
    CheckClass,
    CheckConclusion,
    CheckEntry,
    ClassificationReport,
    ClassifiedCheck,
    Policy,
    classify_check,
    classify_checks,
    parse_check_entry,
    rerun_command,
    should_rerun,
)
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
from worktrees_hives.stacks import (
    DEFAULT_ALLOWED_OWNERS,
    PRInfo,
    Stack,
    StackDetector,
    StackMember,
    StackType,
    find_standalone_prs,
    load_allowed_owners_from_env,
    order_prs_bottom_up,
    resolve_allowed_owners,
)
from worktrees_hives.stacks import PRState as StackPRState

__all__ = [
    "DEFAULT_ALLOWED_OWNERS",
    "AttributionConfig",
    "AttributionPlacement",
    "BabysitCycle",
    "BabysitResult",
    "CheckClass",
    "CheckConclusion",
    "CheckEntry",
    "CheckRun",
    "ClassificationReport",
    "ClassifiedCheck",
    "ErrorResponse",
    "Orchestrator",
    "OrchestratorReport",
    "PRInfo",
    "PRState",
    "Policy",
    "PolicyError",
    "ReplyTemplate",
    "Response",
    "ReviewThread",
    "Stack",
    "StackDetector",
    "StackMember",
    "StackPRState",
    "StackType",
    "SuccessResponse",
    "ThreadAction",
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
    "babysit_multiple",
    "classify_check",
    "classify_checks",
    "classify_pr",
    "find_standalone_prs",
    "format_attribution",
    "format_reply",
    "load_allowed_owners_from_env",
    "order_prs_bottom_up",
    "parse_check_entry",
    "rerun_command",
    "resolve_allowed_owners",
    "should_rerun",
]
