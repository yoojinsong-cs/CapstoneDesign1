import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from scheduler.controller import class_command, observations
from scheduler.installer import SchedulerConfig
from scheduler.mock_classifier import FlowKey, FlowType, MockClassifier


class ProtocolTests(unittest.TestCase):
    def test_observations_filter_interfaces_and_keep_duplicate_ids_separate(self):
        rows = ('PORT 10 2 0 0 0 0 0 0 0\n'
                'FLOW 10 42 20.0.1.1 1 999999 1000000000 0 2 100 0 0 0 0\n'
                'FLOW 10 43 20.0.1.2 1 1000000 1000000000 0 3 200 0 0 0 0\n'
                'FLOW 11 44 20.0.1.3 1 1000000 1000000000 0 3 200 0 0 0 0\n')
        first, second = list(observations(rows, {10}))
        self.assertEqual(first.flow_id, second.flow_id)
        self.assertNotEqual(first.cookie, second.cookie)
        self.assertEqual(class_command(first, FlowType.MICE), 'class 10 42 1\n')
        self.assertEqual(class_command(second, FlowType.ELEPHANT), 'class 10 43 2\n')

    def test_config_serializes_kernel_protocol(self):
        self.assertEqual(SchedulerConfig().command(10),
                         'config 10 10000000 2 1514 4096 256\n')
        for values in (dict(mode='bad'), dict(delay_seconds=float('nan')),
                       dict(per_flow_limit=5000), dict(quantum_bytes=0)):
            with self.assertRaises(ValueError):
                SchedulerConfig(**values)

    def test_controller_process_delivers_delayed_decision_and_flushes_stats(self):
        with tempfile.TemporaryDirectory(prefix='flowprio-controller-') as tmp:
            root = Path(tmp)
            proc, stats, ready = root / 'proc', root / 'stats', root / 'ready'
            first_ns = time.monotonic_ns()
            proc.write_text('FLOW 10 42 20.0.1.1 1 1000000 %d 0 2 100 0 0 0 0\n'
                            % first_ns)
            stats.write_text('PORT 10 2 1000 0 0 0 0 0 0\n')
            process = subprocess.Popen([
                sys.executable, '-m', 'scheduler.controller', '--interfaces', '10',
                '--run-id', 'test', '--logdir', str(root), '--ready', str(ready),
                '--proc', str(proc), '--stats', str(stats)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not proc.read_text().startswith('class '):
                    if process.poll() is not None:
                        self.fail(process.communicate()[1])
                    if time.monotonic() > deadline:
                        self.fail('controller did not deliver a decision')
                    time.sleep(0.01)
                self.assertEqual(proc.read_text(), 'class 10 42 2\n')
                self.assertTrue(ready.exists())
                process.terminate()
                out, err = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, err)
                events = [json.loads(line) for line in
                          (root / 'classifications.jsonl').read_text().splitlines()]
                decision = next(row for row in events if row['event'] == 'decision')
                self.assertEqual(decision['flow_type'], 'ELEPHANT')
                self.assertGreaterEqual(decision['delay'], 0.5)
                self.assertEqual((root / 'final_stats.txt').read_text(), stats.read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


class TimestampTests(unittest.IsolatedAsyncioTestCase):
    async def test_delay_is_based_on_switch_observation_not_poll_time(self):
        classifier = MockClassifier()
        self.addAsyncCleanup(classifier.aclose)
        original = time.monotonic() - 2
        decision = await asyncio.wait_for(classifier.submit(
            FlowKey('run', '20.0.1.1', 1), 10, lambda result: None,
            observed_at=original), timeout=0.5)
        self.assertEqual(decision.observed_at, original)
        self.assertEqual(decision.flow_type, FlowType.MICE)


@unittest.skipUnless(shutil.which('gcc'), 'gcc is needed for kernel-source harness')
class KernelPolicyTests(unittest.TestCase):
    def test_actual_kernel_source_parser_and_policy_with_sanitizers(self):
        source = Path(__file__).with_name('kernel_policy_test.c')
        with tempfile.TemporaryDirectory(prefix='flowprio-policy-') as tmp:
            binary = str(Path(tmp) / 'policy-test')
            subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wno-unused-function',
                            '-Wno-unused-variable', '-fsanitize=address,undefined',
                            '-g', str(source), '-o', binary], check=True,
                           capture_output=True, text=True)
            environment = dict(os.environ, ASAN_OPTIONS='detect_leaks=0',
                               UBSAN_OPTIONS='halt_on_error=1')
            result = subprocess.run([binary], env=environment, check=True,
                                    capture_output=True, text=True)
            self.assertIn('11 actual kernel-source policy/parser tests passed',
                          result.stdout)
