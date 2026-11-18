import csv
import json
from pathlib import Path
import tempfile
import unittest

from scheduler.qos import flow_metrics, groups, save_run_qos, write_qos_outputs


class QosTests(unittest.TestCase):
    def write_client(self, directory, index, rows, window=None):
        (directory / ('flows_%d.txt' % index)).write_text(
            ''.join('%d %d 0 0 0\n' % row for row in rows))
        (directory / ('log_%d.txt' % index)).write_text(
            'FLOW_TIMING_NS %d %d\n' % window if window else 'old client log\n')

    def test_fct_percentiles_float_goodput_and_optional_deadline(self):
        rows = [dict(size_bytes=100, fct_us=n * 1000) for n in range(1, 101)]
        metrics = flow_metrics(rows, 95)
        self.assertEqual(metrics['mean_fct_ms'], 50.5)
        self.assertEqual(metrics['p50_fct_ms'], 50)
        self.assertEqual(metrics['p95_fct_ms'], 95)
        self.assertEqual(metrics['p99_fct_ms'], 99)
        self.assertEqual(metrics['deadline_met_flows'], 95)
        self.assertEqual(metrics['deadline_miss_pct'], 5)
        self.assertAlmostEqual(metrics['p05_flow_goodput_mbps'], 800 / 96000)
        self.assertIsNone(flow_metrics(rows)['deadline_miss_pct'])

    def test_class_boundary_empty_group_and_zero_duration(self):
        rows = [dict(size_bytes=999999, fct_us=0),
                dict(size_bytes=1000000, fct_us=2000)]
        metrics = groups(rows, 1000000)
        self.assertEqual(metrics['MICE']['completed_flows'], 1)
        self.assertEqual(metrics['ELEPHANT']['completed_flows'], 1)
        self.assertEqual(metrics['ALL']['zero_duration_flows'], 1)
        self.assertEqual(metrics['ALL']['goodput_samples'], 1)
        self.assertIsNone(metrics['MICE']['mean_flow_goodput_mbps'])
        self.assertIsNone(flow_metrics([])['p99_fct_ms'])

    def test_receiver_rate_uses_actual_global_and_per_client_windows(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.write_client(root, 0, [(100, 2000)], (1000000000, 3000000000))
            self.write_client(root, 1, [(200, 4000)], (2000000000, 6000000000))
            qos = save_run_qos(root, 2, 1, 1000000, dict(transfer_time_seconds=5))
            self.assertAlmostEqual(qos['receiver_goodput_mbps'], 300 * 8 / 5e6)
            self.assertAlmostEqual(qos['clients']['client1']['receiver_goodput_mbps'], .0004)
            self.assertEqual(qos['clients']['client2']['active_window_seconds'], 4)
            self.assertEqual(qos['groups']['ALL']['mean_flow_goodput_mbps'], .4)
            self.assertIn('packet_loss_rate', qos['unmeasured'])
            self.assertEqual(json.loads((root / 'qos.json').read_text()), qos)

    def test_incomplete_runs_and_invalid_deadlines_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.write_client(root, 0, [(100, 2000)])
            for deadline in (0, -1, float('nan'), float('inf')):
                with self.assertRaises(ValueError):
                    save_run_qos(root, 1, 1, 1000000,
                                 dict(transfer_time_seconds=1), deadline)
            with self.assertRaisesRegex(ValueError, 'completed run'):
                save_run_qos(root, 1, 2, 1000000, dict(transfer_time_seconds=1))
            self.assertFalse((root / 'qos.json').exists())

    def test_summary_pools_flows_and_csv_separates_clients_and_classes(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            results = []
            for repeat, delays in ((1, [1000, 2000]), (2, [1000, 1000000])):
                directory = root / ('%02d_priority' % repeat)
                directory.mkdir()
                self.write_client(directory, 0, [(100, delay) for delay in delays])
                timing = dict(policy='priority', repeat=repeat, seed=repeat,
                              transfer_time_seconds=repeat)
                save_run_qos(directory, 1, 2, 1000000, timing)
                results.append(timing)
            write_qos_outputs(root, results, 1, 2, 1000000)
            report = (root / 'qos_summary.txt').read_text()
            self.assertIn('priority ALL 4 251.000 1000.000 1000.000 1000.000', report)
            with (root / 'qos.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 12)
            self.assertEqual({row['scope'] for row in rows}, {'ALL_CLIENTS', 'client1'})
            mice = next(row for row in rows if row['flow_type'] == 'MICE')
            self.assertEqual(mice['receiver_goodput_mbps'], '')
            client = next(row for row in rows if row['scope'] == 'client1')
            self.assertEqual(client['receiver_goodput_mbps'], '')


if __name__ == '__main__':
    unittest.main()
