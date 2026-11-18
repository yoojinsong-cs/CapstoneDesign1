"""Return a ground-truth size class after an independent delay per flow.

Call submit() when the switch observes a flow's first application-data packet.
The caller must run an asyncio event loop alongside packet processing: do not
await the returned task in the packet-forwarding path. The callback is the
future scheduler's priority-update interface, not a packet sender.
"""

import asyncio
import inspect
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, Tuple


DEFAULT_CLASSIFIER_DELAY = 0.5


class FlowType(str, Enum):
    MICE = "MICE"
    ELEPHANT = "ELEPHANT"


@dataclass(frozen=True)
class FlowKey:
    run_id: str
    client_ip: str
    flow_id: int

    def __post_init__(self):
        if not self.run_id or not self.client_ip:
            raise ValueError("run_id and client_ip are required")
        if not isinstance(self.flow_id, int) or self.flow_id <= 0:
            raise ValueError("flow_id must be positive; ID 0 is a control request")


@dataclass(frozen=True)
class Classification:
    key: FlowKey
    flow_type: FlowType
    observed_at: float
    decided_at: float


DecisionCallback = Callable[[Classification], Awaitable[None] | None]


class MockClassifier:
    """An asyncio-owned delayed classifier with no forwarding side effects.

    flow_size is private ground truth. Decisions contain only identity, class,
    and monotonic timestamps; the scheduler does not receive the exact size.
    Call all methods on the same running event loop. Await submitted tasks (or
    otherwise retrieve their exceptions) to surface callback failures.
    """

    def __init__(self, delay_seconds: float = DEFAULT_CLASSIFIER_DELAY,
                 threshold_bytes: int = 1_000_000):
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and nonnegative")
        if not isinstance(threshold_bytes, int) or threshold_bytes <= 0:
            raise ValueError("threshold_bytes must be a positive integer")
        self.delay_seconds = delay_seconds
        self.threshold_bytes = threshold_bytes
        self._pending: Dict[
            FlowKey, Tuple[int, asyncio.Task[Classification]]
        ] = {}
        self._closed = False

    def submit(self, key: FlowKey, flow_size: int,
               on_decision: DecisionCallback, *,
               observed_at: float | None = None) -> asyncio.Task[Classification]:
        """Schedule once per pending flow; duplicates do not reset its timer.

        The observer must register each logical flow only once after a decision
        has been delivered. Subsequent data/retransmissions are not new flows.
        """
        if self._closed:
            raise RuntimeError("classifier is closed")
        if not isinstance(flow_size, int) or flow_size < 0:
            raise ValueError("flow_size must be a nonnegative integer")
        if not callable(on_decision):
            raise TypeError("on_decision must be callable")
        if key in self._pending:
            previous_size, task = self._pending[key]
            if previous_size != flow_size:
                raise ValueError("conflicting size for the same pending flow")
            return task

        now = time.monotonic()
        if observed_at is None:
            observed_at = now
        if not math.isfinite(observed_at) or observed_at < 0 or observed_at > now:
            raise ValueError("observed_at must be a past monotonic timestamp")
        task = asyncio.create_task(
            self._classify(key, flow_size, observed_at, on_decision))
        self._pending[key] = (flow_size, task)
        return task

    async def _classify(self, key: FlowKey, flow_size: int,
                        observed_at: float,
                        on_decision: DecisionCallback) -> Classification:
        try:
            # Account for time already spent waiting for this task to run.
            deadline = observed_at + self.delay_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            flow_type = (FlowType.ELEPHANT if flow_size >= self.threshold_bytes
                         else FlowType.MICE)
            decision = Classification(
                key, flow_type, observed_at, time.monotonic())
            callback_result = on_decision(decision)
            if inspect.isawaitable(callback_result):
                await callback_result
            return decision
        finally:
            pending = self._pending.get(key)
            if pending is not None and pending[1] is asyncio.current_task():
                del self._pending[key]

    def discard(self, key: FlowKey) -> bool:
        """Cancel a pending decision for a completed/aborted logical flow.

        A temporarily empty switch queue is not proof that a flow completed.
        """
        pending = self._pending.pop(key, None)
        if pending is None:
            return False
        pending[1].cancel()
        return True

    async def aclose(self):
        """Cancel and join pending tasks before stopping the event loop."""
        self._closed = True
        tasks = [task for _, task in self._pending.values()]
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
