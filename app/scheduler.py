"""Background recompute scheduler.

A recompute is expensive, so it never runs inside the upload request.  Each
wafer has at most one recompute in flight; extra requests collapse into a
"run again when the current one finishes" flag.  The sequence guard in
``WaferService._publish`` is what actually protects correctness, so even if
two workers ever raced, a stale snapshot could not be published.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .service import WaferService

logger = logging.getLogger(__name__)


class RecomputeScheduler:
    def __init__(self, service: WaferService, *, workers: int = 2) -> None:
        self._service = service
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="recompute"
        )
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._rerun: set[str] = set()
        self._closed = False

    def submit(self, wafer_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            if wafer_id in self._inflight:
                self._rerun.add(wafer_id)
                return
            self._inflight.add(wafer_id)
        try:
            self._executor.submit(self._run, wafer_id)
        except RuntimeError:  # executor already shut down
            with self._lock:
                self._inflight.discard(wafer_id)

    def _run(self, wafer_id: str) -> None:
        try:
            while True:
                try:
                    self._service.recompute(wafer_id)
                except Exception:
                    logger.exception("recompute failed for wafer %s", wafer_id)
                    rerun = False
                else:
                    # ``recompute`` gives up after a fixed number of snapshot
                    # retries.  If uploads are still landing, keep draining
                    # them instead of leaving the wafer pending without a worker.
                    try:
                        state = self._service.get_wafer_state(wafer_id)
                    except Exception:
                        logger.exception("cannot read state for wafer %s", wafer_id)
                        rerun = False
                    else:
                        rerun = int(state["accepted_seq"]) > int(state["committed_seq"])
                with self._lock:
                    if rerun or wafer_id in self._rerun:
                        self._rerun.discard(wafer_id)
                        if self._closed:
                            self._inflight.discard(wafer_id)
                            return
                        continue
                    self._inflight.discard(wafer_id)
                    return
        finally:
            # Never leave the wafer wedged if something unexpected propagated.
            with self._lock:
                self._inflight.discard(wafer_id)

    def recover_pending(self) -> None:
        """Requeue work that was interrupted by a crash or restart."""
        for wafer_id in self._service.pending_wafer_ids():
            self.submit(wafer_id)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
