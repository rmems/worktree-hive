"""CI check taxonomy for worktrees-hives PR babysit.

Classifies CI checks from ``gh pr checks --json`` into three classes:

- **Class A** — GitHub Actions workflows that the orchestrator owns and can fix.
- **Class B** — Codacy quality gates (linters, static analysis) that the
  orchestrator can fix by editing source files.
- **Class C** — Third-party review bots (Dependabot, Codecov, etc.) and
  unrecognised providers that the orchestrator cannot fix directly; it can
  only report or re-trigger.

The classification rules are deterministic string-matching heuristics applied
to the ``name``, ``workflow``/``workflowName``, and ``description`` fields of
each check entry.  Agents call :func:`classify_check` on every entry from
``gh pr checks --json`` to get a :class:`CheckClass` and a set of
:class:`Policy` flags.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any


class CheckClass(enum.Enum):
    A = "A"
    B = "B"
    C = "C"


class CheckConclusion(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    SKIPPED = "skipped"
    STALE = "stale"
    PENDING = "pending"
    STARTUP_FAILURE = "startup_failure"


class Policy(enum.Flag):
    NONE = 0
    RERUN = enum.auto()
    FIX_SOURCE = enum.auto()
    REPLY_WITH_SHA = enum.auto()
    MARK_RESIDUAL = enum.auto()
    REPORT_ONLY = enum.auto()
    FORBID_EMPTY_COMMIT = enum.auto()
    FORBID_IGNORE = enum.auto()


_CLASS_A_WORKFLOW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bci\b", re.IGNORECASE),
    re.compile(r"\bbuild\b", re.IGNORECASE),
    re.compile(r"\btest(s)?\b", re.IGNORECASE),
    re.compile(r"\blint\b", re.IGNORECASE),
    re.compile(r"\brust\b", re.IGNORECASE),
    re.compile(r"\bpython\b", re.IGNORECASE),
    re.compile(r"\bcheck(s)?\b", re.IGNORECASE),
    re.compile(r"\bquality\b", re.IGNORECASE),
    re.compile(r"github.actions", re.IGNORECASE),
]

_CLASS_B_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"codacy", re.IGNORECASE),
    re.compile(r"code\s*climate", re.IGNORECASE),
    re.compile(r"sonar(cloud|qube)?", re.IGNORECASE),
    re.compile(r"deepsource", re.IGNORECASE),
    re.compile(r"code\s*analysis", re.IGNORECASE),
]

_CLASS_C_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"dependabot", re.IGNORECASE),
    re.compile(r"renovate", re.IGNORECASE),
    re.compile(r"codecov", re.IGNORECASE),
    re.compile(r"coveralls", re.IGNORECASE),
    re.compile(r"\bsnyk\b", re.IGNORECASE),
    re.compile(r"review\s*bot", re.IGNORECASE),
    re.compile(r"\bcopilot\b", re.IGNORECASE),
    re.compile(r"code\s*review", re.IGNORECASE),
]


def _matches_any(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


@dataclass(frozen=True, slots=True)
class CheckEntry:
    name: str
    workflow_name: str
    conclusion: CheckConclusion
    status: str
    description: str
    details_url: str
    run_id: int | None


# Map gh CLI ``bucket`` values and extra ``state`` aliases onto CheckConclusion.
# See: https://cli.github.com/manual/gh_pr_checks and cli aggregate.go fail states.
_CONCLUSION_ALIASES: dict[str, str] = {
    "pass": "success",
    "fail": "failure",
    "pending": "pending",
    "skipping": "skipped",
    "cancel": "cancelled",
    "cancelled": "cancelled",
    "error": "failure",  # GitHub check state ERROR is a failing outcome
    "success": "success",
    "failure": "failure",
    "neutral": "neutral",
    "timed_out": "timed_out",
    "action_required": "action_required",
    "skipped": "skipped",
    "stale": "stale",
    "startup_failure": "startup_failure",
}


def _normalize_conclusion(value: str) -> CheckConclusion:
    """Normalize conclusion/state/bucket strings to :class:`CheckConclusion`."""
    key = value.strip().lower()
    mapped = _CONCLUSION_ALIASES.get(key, key)
    try:
        return CheckConclusion(mapped)
    except ValueError:
        return CheckConclusion.PENDING


def _is_github_actions_url(url: str) -> bool:
    """True when *url* points at a GitHub Actions run (required for Class A)."""
    return bool(url and re.search(r"/actions/runs/\d+", url))


def parse_check_entry(raw: dict[str, Any]) -> CheckEntry:
    """Parse a raw check dict from ``gh pr checks --json``.

    Supports both ``state``/``bucket``/``workflow``/``link`` (actual gh output)
    and ``conclusion``/``workflowName``/``detailsUrl`` (legacy names).

    ``bucket`` values (``pass``/``fail``/``pending``/``skipping``/``cancel``) and
    GitHub ``state: ERROR`` are normalized to :class:`CheckConclusion` members.
    """
    name = raw.get("name") or ""
    workflow_name = raw.get("workflow") or raw.get("workflowName") or ""
    description = raw.get("description") or ""
    details_url = raw.get("link") or raw.get("detailsUrl") or ""

    conclusion_str = raw.get("conclusion") or raw.get("state") or raw.get("bucket") or "pending"
    conclusion = _normalize_conclusion(str(conclusion_str))

    status = raw.get("status") or "pending"

    run_id: int | None = None
    if details_url:
        m = re.search(r"/actions/runs/(\d+)", details_url)
        if m:
            run_id = int(m.group(1))

    return CheckEntry(
        name=name,
        workflow_name=workflow_name,
        conclusion=conclusion,
        status=status,
        description=description,
        details_url=details_url,
        run_id=run_id,
    )


@dataclass(frozen=True, slots=True)
class ClassifiedCheck:
    entry: CheckEntry
    check_class: CheckClass
    policies: frozenset[Policy]
    reason: str


def _classify_class(entry: CheckEntry) -> CheckClass:
    text = f"{entry.name} {entry.workflow_name} {entry.description}"
    if _matches_any(text, _CLASS_B_NAME_PATTERNS):
        return CheckClass.B
    if _matches_any(text, _CLASS_C_NAME_PATTERNS):
        return CheckClass.C
    # Owned GitHub Actions runs are Class A even when the check name does not
    # match a name heuristic (e.g. workflow "Security", job "cargo audit").
    # Require an Actions run URL so third-party CI (Circle, Travis, …) stays C.
    if _is_github_actions_url(entry.details_url):
        return CheckClass.A
    if _matches_any(text, _CLASS_A_WORKFLOW_PATTERNS):
        # Name looks like CI but no Actions run URL → not owned/fixable.
        return CheckClass.C
    # Default to C (report-only) for unknown / non-Actions providers.
    return CheckClass.C


def _policies_for_class(
    check_class: CheckClass,
    conclusion: CheckConclusion,
    has_run_id: bool = False,
) -> frozenset[Policy]:
    # Terminal-passing and non-terminal pending: no fix/rerun/residual yet.
    if conclusion in (
        CheckConclusion.SUCCESS,
        CheckConclusion.SKIPPED,
        CheckConclusion.NEUTRAL,
        CheckConclusion.PENDING,
    ):
        return frozenset()

    base: set[Policy] = {Policy.FORBID_IGNORE}

    if check_class == CheckClass.A:
        base.update(
            {
                Policy.FIX_SOURCE,
                Policy.RERUN,
                Policy.REPLY_WITH_SHA,
                Policy.FORBID_EMPTY_COMMIT,
            }
        )
    elif check_class == CheckClass.B:
        base.update(
            {
                Policy.FIX_SOURCE,
                Policy.REPLY_WITH_SHA,
                Policy.FORBID_EMPTY_COMMIT,
            }
        )
    elif check_class == CheckClass.C:
        base.update({Policy.REPORT_ONLY, Policy.MARK_RESIDUAL})
        if has_run_id:
            base.add(Policy.RERUN)

    return frozenset(base)


def classify_check(entry: CheckEntry) -> ClassifiedCheck:
    check_class = _classify_class(entry)
    policies = _policies_for_class(
        check_class, entry.conclusion, has_run_id=entry.run_id is not None
    )
    reason = _explain_classification(entry, check_class)
    return ClassifiedCheck(
        entry=entry,
        check_class=check_class,
        policies=policies,
        reason=reason,
    )


def _explain_classification(entry: CheckEntry, check_class: CheckClass) -> str:
    if check_class == CheckClass.A:
        return f"GitHub Actions workflow '{entry.name}' — fixable by agent"
    if check_class == CheckClass.B:
        return f"Quality gate '{entry.name}' — fixable via source edits"
    return f"Third-party bot '{entry.name}' — report or mark residual"


@dataclass
class ClassificationReport:
    checks: list[ClassifiedCheck] = field(default_factory=list)

    @property
    def class_a(self) -> list[ClassifiedCheck]:
        return [c for c in self.checks if c.check_class == CheckClass.A]

    @property
    def class_b(self) -> list[ClassifiedCheck]:
        return [c for c in self.checks if c.check_class == CheckClass.B]

    @property
    def class_c(self) -> list[ClassifiedCheck]:
        return [c for c in self.checks if c.check_class == CheckClass.C]

    @property
    def failures(self) -> list[ClassifiedCheck]:
        return [
            c
            for c in self.checks
            if c.entry.conclusion
            in (
                CheckConclusion.FAILURE,
                CheckConclusion.TIMED_OUT,
                CheckConclusion.STARTUP_FAILURE,
                CheckConclusion.ACTION_REQUIRED,
                CheckConclusion.CANCELLED,
                CheckConclusion.STALE,
            )
        ]

    @property
    def fixable_failures(self) -> list[ClassifiedCheck]:
        return [c for c in self.failures if c.check_class in (CheckClass.A, CheckClass.B)]

    @property
    def residual_failures(self) -> list[ClassifiedCheck]:
        return [c for c in self.failures if c.check_class == CheckClass.C]

    @property
    def all_passed(self) -> bool:
        """True if there is at least one check and every check is terminal-passing.

        An empty check list is not treated as success (unknown / no data).
        """
        if not self.checks:
            return False
        terminal_passing = {
            CheckConclusion.SUCCESS,
            CheckConclusion.SKIPPED,
            CheckConclusion.NEUTRAL,
        }
        return all(c.entry.conclusion in terminal_passing for c in self.checks)


def classify_checks(raw_checks: list[dict[str, Any]]) -> ClassificationReport:
    report = ClassificationReport()
    for raw in raw_checks:
        entry = parse_check_entry(raw)
        classified = classify_check(entry)
        report.checks.append(classified)
    return report


def should_rerun(classified: ClassifiedCheck) -> bool:
    """Rerun only transient failures; Class C also requires run_id."""
    if classified.check_class == CheckClass.B:
        return False
    if Policy.RERUN not in classified.policies:
        return False
    if classified.entry.conclusion in (
        CheckConclusion.TIMED_OUT,
        CheckConclusion.STARTUP_FAILURE,
        CheckConclusion.CANCELLED,
    ):
        if classified.check_class == CheckClass.C:
            return classified.entry.run_id is not None
        return True
    return False


def rerun_command(classified: ClassifiedCheck) -> list[str] | None:
    """Return *subcommand* args for ``WhClient.run`` / ``wh --json``.

    Returns ``["ci", "rerun", "<run_id>"]`` (no binary name). Callers pass this
    to :meth:`~worktrees_hives.bridge.WhClient.run`, which prepends the resolved
    ``wh`` path and ``--json``. Do not include a leading ``"wh"`` token.

    Reruns go through the Rust ``wh`` boundary (not raw ``gh``).
    """
    if not should_rerun(classified):
        return None
    if classified.entry.run_id is None:
        return None
    return ["ci", "rerun", str(classified.entry.run_id)]
