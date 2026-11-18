import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, mock_open, patch

from scheduler.benchmark import metrics, plan_runs, read_flows, run
from scheduler.installer import SchedulerConfig, SwitchScheduler


class BenchmarkTests(unittest.TestCase):
    def test_cleanup_does_not_use_the_busy_client_shell(self):
        from flowGenerator_parll import stopManagedProcesses
        host = MagicMock()
        host.cmd.side_effect = AssertionError('client still owns this shell')
        stopManagedProcesses([(host, 1234)], 'TERM')
        host.popen.assert_called_once_with(['kill', '-TERM', '1234'])
        host.popen.return_value.wait.assert_called_once_with(timeout=5)

    def test_runner_preserves_ten_separate_runs_and_reports_completed_counts(self):
        def simulated_experiment(command, **kwargs):
            directory = Path(command[command.index('--run-dir') + 1])
            directory.mkdir()
            for client in range(2):
                (directory / ('flows_%d.txt' % client)).write_text(
                    '999999 1000 0 0 1\n' * 25 + '1000000 9000 0 0 1\n' * 25)
            if command[command.index('--scheduler') + 1] == 'priority':
                (directory / 'classifications.jsonl').write_text(
                    '{"event": "decision", "delay": 0.51}\n')
            process = MagicMock(returncode=0)
            process.poll.return_value = 0
            return process

        with tempfile.TemporaryDirectory() as name:
            args = SimpleNamespace(output=name, flows=100, repeats=5, seed=1729,
                                   prepare_only=False)
            with patch('scheduler.benchmark.os.geteuid', return_value=0), \
                 patch('scheduler.benchmark.shutil.which', return_value='/usr/bin/tool'), \
                 patch('scheduler.benchmark.subprocess.check_output', return_value=''), \
                 patch('scheduler.benchmark.ensure_module'), \
                 patch('scheduler.benchmark.restore_owner'), \
                 patch('scheduler.benchmark.subprocess.Popen', side_effect=simulated_experiment), \
                 patch('sys.stdout', new_callable=io.StringIO):
                run(args)
            output = Path(name)
            status = json.loads((output / 'status.json').read_text())
            self.assertEqual(status['status'], 'complete')
            self.assertEqual(status['completed_runs'], 10)
            self.assertEqual(len(list(output.glob('*/flows_*.txt'))), 20)
            rows = json.loads((output / 'results.json').read_text())
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row['metrics']['ALL']['n'] == 100 for row in rows))

    def test_pairs_use_same_seed_and_original_topology_for_exactly_100_flows(self):
        runs = plan_runs(Path('/tmp/comparison'))
        self.assertEqual(len(runs), 10)
        self.assertEqual(sum(r['mode'] == 'priority' for r in runs), 5)
        for first, second in zip(runs[::2], runs[1::2]):
            self.assertEqual(first['seed'], second['seed'])
            self.assertEqual({first['mode'], second['mode']}, {'priority', 'legacy'})
            for run in (first, second):
                self.assertEqual(run['command'][3:10], ['2', '2', '1', '1', '2', '5001', '50'])
                delay = run['command'].index('--classifier-delay')
                self.assertEqual(run['command'][delay + 1], '0.5')

    def test_incomplete_client_is_rejected_before_reporting_metrics(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'flows_0.txt').write_text('999999 1000 0 0 1\n')
            (root / 'flows_1.txt').write_text('1000000 9000 0 0 1\n')
            result = metrics(read_flows(root, 2))
            self.assertEqual(result['ALL']['mean_ms'], 5)
            self.assertEqual(result['MICE']['n'], 1)
            self.assertEqual(result['ELEPHANT']['mean_ms'], 9)
            with self.assertRaises(ValueError):
                read_flows(root, 4)

    def test_original_topology_keeps_both_10mbps_paths(self):
        from topoBuilder_lb import topoBuilder
        with patch('topoBuilder_lb.Mininet') as network:
            topoBuilder(2, 2, 1, 1, 1, 1, 2)
        topo = network.call_args.kwargs['topo']
        edge_links = [(a, b, info['bw']) for a, b, info in topo.links(withInfo=True)
                      if a.startswith('core') or b.startswith('core')]
        self.assertEqual(len(edge_links), 4)
        self.assertTrue(all(bw == 10 for _, _, bw in edge_links))
        self.assertEqual(len(topo.hosts()), 4)
        self.assertEqual(len(topo.switches()), 6)


class MultiPortInstallerTests(unittest.TestCase):
    def network(self):
        network = MagicMock()
        network.switches = [SimpleNamespace(name='Edges1'), SimpleNamespace(name='core1'),
                            SimpleNamespace(name='core2')]
        network.get.return_value.connectionsTo.side_effect = [
            [(SimpleNamespace(name='Edges1-eth1'), None)],
            [(SimpleNamespace(name='Edges1-eth2'), None)]]
        return network

    def test_both_outputs_are_configured_observed_and_removed(self):
        process = MagicMock()
        process.poll.return_value = None

        def start_process(command, **kwargs):
            Path(command[command.index('--ready') + 1]).write_text('ready\n')
            return process

        with tempfile.TemporaryDirectory() as name:
            scheduler = SwitchScheduler(SchedulerConfig(), Path(name) / 'run')
            with patch('scheduler.installer.socket.if_nametoindex', side_effect=[11, 12]), \
                 patch('scheduler.installer.subprocess.run') as command, \
                 patch('scheduler.installer.subprocess.Popen', side_effect=start_process) as spawn, \
                 patch('scheduler.installer.open', mock_open(), create=True) as control, \
                 patch('sys.stdout', new_callable=io.StringIO):
                scheduler.start(self.network())
                argv = spawn.call_args.args[0]
                offset = argv.index('--interfaces')
                self.assertEqual(argv[offset + 1:offset + 3], ['11', '12'])
                writes = [call.args[0] for call in control().write.call_args_list]
                self.assertEqual(writes, [SchedulerConfig().command(11), SchedulerConfig().command(12)])
                installs = [call.args[0] for call in command.call_args_list
                            if call.args[0][:3] == ['tc', 'qdisc', 'replace']]
                self.assertEqual(installs, [
                    ['tc', 'qdisc', 'replace', 'dev', 'Edges1-eth1',
                     'parent', '5:1', 'handle', '10:', 'flowprio'],
                    ['tc', 'qdisc', 'replace', 'dev', 'Edges1-eth2',
                     'parent', '5:1', 'handle', '10:', 'flowprio'],
                ])
                manifest = json.loads((scheduler.directory / 'manifest.json').read_text())
                self.assertEqual([p['ifindex'] for p in manifest['interfaces']], [11, 12])
                scheduler.stop()
                removals = [call.args[0] for call in command.call_args_list
                            if call.args[0][:3] == ['tc', 'qdisc', 'del']]
                self.assertEqual(removals, [
                    ['tc', 'qdisc', 'del', 'dev', 'Edges1-eth1',
                     'parent', '5:1', 'handle', '10:'],
                    ['tc', 'qdisc', 'del', 'dev', 'Edges1-eth2',
                     'parent', '5:1', 'handle', '10:'],
                ])

    def test_failure_on_second_port_removes_first_qdisc(self):
        def fail_second(command, **kwargs):
            if command[:3] == ['tc', 'qdisc', 'replace'] and command[4] == 'Edges1-eth2':
                raise OSError('second port failed')

        with tempfile.TemporaryDirectory() as name:
            scheduler = SwitchScheduler(SchedulerConfig(), Path(name) / 'run')
            with patch('scheduler.installer.socket.if_nametoindex', side_effect=[11, 12]), \
                 patch('scheduler.installer.subprocess.run', side_effect=fail_second) as command, \
                 patch('scheduler.installer.open', mock_open(), create=True):
                with self.assertRaises(OSError):
                    scheduler.start(self.network())
                self.assertEqual(command.call_args.args[0],
                                 ['tc', 'qdisc', 'del', 'dev', 'Edges1-eth1',
                                  'parent', '5:1', 'handle', '10:'])
                self.assertEqual(scheduler.interfaces, [])
