"""Multi-subagent orchestrator with capped parallelism.

Spawns N worker coroutines with a concurrency cap, monitors their status,
handles timeouts and failures, and aggregates results into a final report.
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class WorkerStatus(enum.Enum):
    """Lifecycle state of a single worker."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Outcome of a single worker execution."""

    worker_id: str
    status: WorkerStatus
    result: Any = None
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class OrchestratorReport:
    """Aggregated results from an orchestrator run."""

    total: int
    succeeded: int
    failed: int
    timed_out: int
    cancelled: int
    elapsed_seconds: float
    results: list[WorkerResult]

    @property
    def all_succeeded(self) -> bool:
        return self.succeeded == self.total


@dataclass
class WorkerSpec:
    """Describes a single worker to be executed."""

    worker_id: str
    fn: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None


class Orchestrator:
    """Runs N workers with a concurrency cap.

    Parameters
    ----------
    concurrency:
        Maximum number of workers running simultaneously.
    default_timeout:
        Per-worker timeout in seconds.  ``None`` means no timeout.
        Individual ``WorkerSpec.timeout`` values override this.
    """

    def __init__(
        self,
        concurrency: int = 4,
        default_timeout: float | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._concurrency = concurrency
        self._default_timeout = default_timeout

    async def run(self, workers: list[WorkerSpec]) -> OrchestratorReport:
        """Execute all workers and return an aggregated report.

        Workers are dispatched subject to the concurrency cap.  Each worker
        is wrapped with timeout and error-handling logic so that one failure
        never cancels siblings.
        """
        if not workers:
            return OrchestratorReport(
                total=0,
                succeeded=0,
                failed=0,
                timed_out=0,
                cancelled=0,
                elapsed_seconds=0.0,
                results=[],
            )

        semaphore = asyncio.Semaphore(self._concurrency)
        start = time.monotonic()

        async def _run_one(spec: WorkerSpec) -> WorkerResult:
            return await self._execute_worker(spec, semaphore)

        results = await asyncio.gather(
            *(_run_one(w) for w in workers),
            return_exceptions=False,
        )

        elapsed = time.monotonic() - start
        return self._build_report(list(results), elapsed)

    async def _execute_worker(
        self,
        spec: WorkerSpec,
        semaphore: asyncio.Semaphore,
    ) -> WorkerResult:
        """Run a single worker under the semaphore with timeout handling."""
        timeout = spec.timeout if spec.timeout is not None else self._default_timeout
        start = time.monotonic()

        async with semaphore:
            try:
                coro = spec.fn(*spec.args, **spec.kwargs)
                result = await asyncio.wait_for(coro, timeout=timeout)
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.SUCCEEDED,
                    result=result,
                    elapsed_seconds=time.monotonic() - start,
                )
            except asyncio.TimeoutError:
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.TIMED_OUT,
                    error=f"Worker timed out after {timeout}s",
                    elapsed_seconds=time.monotonic() - start,
                )
            except asyncio.CancelledError:
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.CANCELLED,
                    error="Worker was cancelled",
                    elapsed_seconds=time.monotonic() - start,
                )
            except Exception as exc:
                return WorkerResult(
                    worker_id=spec.worker_id,
                    status=WorkerStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=time.monotonic() - start,
                )

    @staticmethod
    def _build_report(
        results: list[WorkerResult],
        elapsed: float,
    ) -> OrchestratorReport:
        """Tally worker outcomes into a report."""
        succeeded = sum(1 for r in results if r.status == WorkerStatus.SUCCEEDED)
        failed = sum(1 for r in results if r.status == WorkerStatus.FAILED)
        timed_out = sum(1 for r in results if r.status == WorkerStatus.TIMED_OUT)
        cancelled = sum(1 for r in results if r.status == WorkerStatus.CANCELLED)
        return OrchestratorReport(
            total=len(results),
            succeeded=succeeded,
            failed=failed,
            timed_out=timed_out,
            cancelled=cancelled,
            elapsed_seconds=elapsed,
            results=results,
        )
