import asyncio
import unittest

from scheduler.mock_classifier import FlowKey, FlowType, MockClassifier


class MockClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_delay_overlaps_and_does_not_block_other_work(self):
        classifier = MockClassifier()
        self.assertEqual(classifier.delay_seconds, 0.5)
        self.addAsyncCleanup(classifier.aclose)
        decisions = []
        first = classifier.submit(
            FlowKey("run", "20.0.1.1", 1), 999_999, decisions.append)
        second = classifier.submit(
            FlowKey("run", "20.0.1.2", 1), 1_000_000, decisions.append)

        # Packet processing (represented by this coroutine) can continue while
        # both decisions are pending. A blocking sleep would fail this check.
        await asyncio.sleep(0.05)
        self.assertEqual(decisions, [])
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertIsNotNone(first.get_coro().cr_await)
        self.assertIsNotNone(second.get_coro().cr_await)

        mice, elephant = await asyncio.gather(first, second)
        self.assertEqual(mice.flow_type, FlowType.MICE)
        self.assertEqual(elephant.flow_type, FlowType.ELEPHANT)
        self.assertNotEqual(mice.key, elephant.key)
        for decision in decisions:
            self.assertGreaterEqual(
                decision.decided_at - decision.observed_at, 0.5)
            self.assertFalse(hasattr(decision, "flow_size"))

    async def test_duplicate_does_not_schedule_a_second_decision(self):
        classifier = MockClassifier(delay_seconds=0)
        self.addAsyncCleanup(classifier.aclose)
        key = FlowKey("run", "20.0.1.1", 7)
        decisions = []
        task = classifier.submit(key, 50, decisions.append)
        self.assertIs(task, classifier.submit(key, 50, decisions.append))
        with self.assertRaises(ValueError):
            classifier.submit(key, 51, decisions.append)
        await task
        self.assertEqual(len(decisions), 1)

    async def test_completed_flow_does_not_deliver_a_late_decision(self):
        classifier = MockClassifier()
        self.addAsyncCleanup(classifier.aclose)
        key = FlowKey("run", "20.0.1.1", 2)
        decisions = []
        task = classifier.submit(key, 500, decisions.append)
        await asyncio.sleep(0)
        self.assertTrue(classifier.discard(key))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(decisions, [])
        self.assertFalse(classifier.discard(key))

    async def test_async_callback(self):
        classifier = MockClassifier(delay_seconds=0)
        self.addAsyncCleanup(classifier.aclose)
        decisions = []

        async def callback(decision):
            await asyncio.sleep(0)
            decisions.append(decision)

        result = await classifier.submit(
            FlowKey("run", "20.0.1.1", 3), 100, callback)
        self.assertEqual(decisions, [result])

    async def test_callback_error_is_visible_to_task_owner(self):
        classifier = MockClassifier(delay_seconds=0)
        self.addAsyncCleanup(classifier.aclose)

        def callback(decision):
            raise RuntimeError("priority update failed")

        with self.assertRaisesRegex(RuntimeError, "priority update failed"):
            await classifier.submit(
                FlowKey("run", "20.0.1.1", 4), 100, callback)

    async def test_shutdown_cancels_pending_work(self):
        classifier = MockClassifier()
        key = FlowKey("run", "20.0.1.1", 5)
        task = classifier.submit(
            key, 100, lambda result: None)
        await classifier.aclose()
        self.assertTrue(task.cancelled())
        with self.assertRaises(RuntimeError):
            classifier.submit(key, 100, lambda result: None)

    async def test_invalid_control_id_and_configuration(self):
        with self.assertRaises(ValueError):
            FlowKey("run", "20.0.1.1", 0)
        for delay in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                MockClassifier(delay_seconds=delay)
        with self.assertRaises(ValueError):
            MockClassifier(threshold_bytes=0)
        classifier = MockClassifier()
        self.addAsyncCleanup(classifier.aclose)
        with self.assertRaises(ValueError):
            classifier.submit(
                FlowKey("run", "20.0.1.1", 6), -1, lambda result: None)
