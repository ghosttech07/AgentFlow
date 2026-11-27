"""The execution engine: a background loop started in the FastAPI lifespan.

start(): recover_on_startup() (reclaim orphaned RUNNING tasks, re-plan stuck PLANNING), then loop.
tick():
  1. pickup_run_requests  - run=true workflows: start, or begin planning (background job)
  2. reap_expired_leases  - RUNNING tasks with an expired lease -> worker_lost -> retry policy
  3. schedule             - per RUNNING workflow: fail / complete / promote (incl. due RETRYING)
                            / approval gate
  4. claim + dispatch     - claim READY tasks for free slots, run each in a worker coroutine
The loop ticks every ENGINE_POLL_INTERVAL_SECONDS, at the earliest retry due time if sooner,
or immediately when wake() is called. All decisions are read from the DB each tick.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import utcnow
from app.engine import runner_jobs, scheduler
from app.engine.recovery import recover_on_startup
from app.engine.worker import execute_task
from app.llm.base import LLMClient

logger = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.llm = llm
        self.worker_id = self.settings.engine_worker_id
        self.max_concurrency = self.settings.engine_max_concurrency
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._wake = asyncio.Event()
        self._inflight: dict[uuid.UUID, asyncio.Task[Any]] = {}  # task id -> worker coroutine
        self._jobs: dict[uuid.UUID, asyncio.Task[Any]] = {}  # workflow id -> planning job
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.last_tick_at: datetime | None = None
        self.recovery: dict[str, Any] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        from app.audit.jobs import AuditMaintenance

        self._maintenance = AuditMaintenance(self.session_factory, self.settings)  # only for a started engine
        await self.recover()  # before the first tick
        self._loop_task = asyncio.create_task(self._run_loop(), name="agentflow-engine")
        logger.info("engine.started worker_id=%s max_concurrency=%d poll_s=%s",
                    self.worker_id, self.max_concurrency, self.settings.engine_poll_interval_seconds)

    async def recover(self) -> dict[str, Any]:
        """Startup recovery (see app.engine.recovery). Called by start(); public for tests."""
        self.recovery = await recover_on_startup(self.session_factory, self.settings, self.worker_id)
        return self.recovery

    async def stop(self) -> None:
        """Stop claiming, wait up to ENGINE_SHUTDOWN_GRACE_SECONDS for in-flight work, then cancel."""
        self._stopping = True
        self.wake()
        if self._loop_task is not None:
            await asyncio.gather(self._loop_task, return_exceptions=True)
        pending = {*self._inflight.values(), *self._jobs.values()}
        if pending:
            logger.info("engine.draining in_flight=%d grace_s=%s", len(pending),
                        self.settings.engine_shutdown_grace_seconds)
            _, not_done = await asyncio.wait(pending, timeout=self.settings.engine_shutdown_grace_seconds)
            if not_done:
                ids = [k for k, v in {**self._inflight, **self._jobs}.items() if v in not_done]
                for t in not_done:
                    t.cancel()
                await asyncio.gather(*not_done, return_exceptions=True)
                logger.warning(
                    "engine.shutdown_cancelled count=%d ids=%s - tasks stay RUNNING (planning jobs "
                    "stay PLANNING) with their leases; Part 5 crash recovery will reclaim them",
                    len(ids), ids,
                )
        logger.info("engine.stopped worker_id=%s", self.worker_id)

    def wake(self) -> None:
        self._wake.set()

    def interrupt(self, task_ids: list[uuid.UUID]) -> int:
        """Cancel local workers of tasks a pause/cancel already took away in the DB (results are
        discarded: the ownership guard fails). Tasks owned by other processes are not here."""
        hits = [self._inflight[t] for t in task_ids if t in self._inflight]
        for worker in hits:
            worker.cancel()
        if hits:
            logger.info("engine.interrupted count=%d", len(hits))
        return len(hits)

    async def _run_loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                logger.exception("engine.tick_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=await self._sleep_seconds())
            except TimeoutError:
                pass
            self._wake.clear()

    async def _sleep_seconds(self) -> float:
        """Poll interval, or less if a retry becomes due sooner."""
        timeout = self.settings.engine_poll_interval_seconds
        try:
            due = await scheduler.next_retry_due(self.session_factory)
        except Exception:
            logger.exception("engine.next_due_failed")
            return timeout
        if due is not None:
            timeout = min(timeout, max(0.0, (due - utcnow()).total_seconds()))
        return timeout

    # ------------------------------------------------------------------ scheduling

    async def tick(self) -> int:
        """One scheduling pass. Returns the amount of work done (0 = nothing changed)."""
        self.last_tick_at = utcnow()
        work = 0
        if not self._stopping:
            started, to_plan = await scheduler.pickup_run_requests(self.session_factory)
            for workflow_id in to_plan:
                self._spawn(self._jobs, workflow_id,
                            runner_jobs.plan_and_start(self.session_factory, self.llm, workflow_id))
            work += started + len(to_plan)

            # Part 10: re-plans waiting for the planner run as background jobs (like planning).
            from app.services import replan_service

            for workflow_id in await replan_service.claim_requested(self.session_factory):
                self._spawn(self._jobs, workflow_id,
                            replan_service.run_job(self.session_factory, self.llm, workflow_id, self.settings))
                work += 1

        if getattr(self, "_maintenance", None) is not None:  # Part 12: checkpoints, integrity, retention
            await self._maintenance.run()
        work += await scheduler.reap_expired_leases(self.session_factory, self.settings)
        work += await scheduler.schedule_running_workflows(self.session_factory, self.settings)

        free = self.max_concurrency - len(self._inflight)
        if not self._stopping and free > 0:
            async with self.session_factory() as session:
                claimed = await scheduler.claim_tasks(
                    session, self.worker_id, free, self.settings.lease_duration_seconds
                )
            for claim in claimed:
                self._spawn(self._inflight, claim.task_id, self._run_worker(claim))
            work += len(claimed)
        return work

    async def _run_worker(self, claim: scheduler.Claim) -> None:
        async with self._semaphore:
            await execute_task(claim, self.session_factory, self.llm, self.settings, self.worker_id)

    def _spawn(self, registry: dict[uuid.UUID, asyncio.Task[Any]], key: uuid.UUID,
               coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro, name=f"agentflow-{key}")
        registry[key] = task

        def _done(fut: asyncio.Task[Any]) -> None:
            registry.pop(key, None)
            if fut.cancelled():
                logger.warning("engine.job_cancelled id=%s (shutdown: left for recovery; pause/cancel: "
                               "already handled in the DB)", key)
            elif fut.exception() is not None:
                logger.error("engine.job_crashed id=%s", key, exc_info=fut.exception())
            self.wake()

        task.add_done_callback(_done)

    # ------------------------------------------------------------------ test / status helpers

    async def run_until_idle(self, timeout: float = 60.0) -> None:
        """Tick until nothing is running and a tick changes nothing (tests; ENGINE_ENABLED=false)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            work = await self.tick()
            pending = {*self._inflight.values(), *self._jobs.values()}
            if not pending and work == 0:
                due = await scheduler.next_retry_due(self.session_factory)
                if due is None:
                    return
                # A task is waiting for its backoff: sleep until it is due, then continue.
                wait = max(0.0, (due - utcnow()).total_seconds())
                if loop.time() + wait > deadline:
                    raise TimeoutError(f"engine not idle after {timeout}s (retry due in {wait:.1f}s)")
                await asyncio.sleep(wait + 0.01)
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"engine not idle after {timeout}s (in flight: {len(pending)})")
            if pending:
                await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                await asyncio.sleep(0)  # let done-callbacks update the in-flight registries

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "llm_provider": self.settings.llm_provider,
            "single_node": self.settings.engine_single_node,
            "running": self._loop_task is not None and not self._loop_task.done(),
            "worker_id": self.worker_id,
            "max_concurrency": self.max_concurrency,
            "in_flight_task_ids": [str(k) for k in self._inflight],
            "planning_workflow_ids": [str(k) for k in self._jobs],
            "last_tick_at": self.last_tick_at,
        }
