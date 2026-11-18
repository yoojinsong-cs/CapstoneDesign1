import ipaddress
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from experiment import (ExperimentProgress, parse_args, policies_for_repeat,
                        run, run_one, workload)
from flowGenerator_parll import readTransferTiming
from ruleInstaller_lb import build_rules
from scheduler.installer import SchedulerConfig


def topology_graph(server, client, server_edges, client_edges, paths):
    from topoBuilder_lb import topoBuilder
    with patch('topoBuilder_lb.Mininet') as network:
        topoBuilder(server, client, server_edges, client_edges, 1, 1, paths)
    topo = network.call_args.kwargs['topo']
    return {name: {peer: port for port, (peer, _) in ports.items()}
            for name, ports in topo.ports.items()}


class ExperimentTests(unittest.TestCase):
    def test_comparison_modes_select_pairs_and_reject_ambiguous_options(self):
        argv = ['2', '2', '1', '1', '2', '5001', '50']
        for options, odd in (([], ('priority', 'legacy')),
                             (['--compare-fair'], ('fair', 'priority')),
                             (['--diagnose'], ('legacy', 'fifo', 'fair', 'priority'))):
            args = parse_args(argv + options)
            self.assertEqual(policies_for_repeat(args, 1), odd)
            self.assertEqual(policies_for_repeat(args, 2), tuple(reversed(odd)))
        for options in (['--compare-fair', '--diagnose'],
                        ['--compare-fair', '--policy', 'priority']):
            with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                parse_args(argv + options)

    def test_fair_comparison_runs_only_the_pair_on_fixed_paths_and_saves_qos(self):
        with tempfile.TemporaryDirectory() as name:
            output = Path(name) / 'comparison'
            args = parse_args(['2', '2', '1', '1', '2', '5001', '2',
                               '--compare-fair', '--repeat', '2', '--output-dir', str(output)])
            network = MagicMock()
            timing = dict(completed_flows=4, first_request_ns=1000000000,
                          last_completion_ns=4000000000, transfer_time_seconds=3)

            def copy_log(source, destination):
                destination.write_text('100 200 0 0 1\n1000000 300 0 0 1\n'
                                       if destination.name.startswith('flows_') else
                                       'FLOW_TIMING_NS 1000000000 4000000000\n')

            with patch('experiment.os.geteuid', return_value=0), \
                 patch('experiment.os.chdir'), patch('experiment.subprocess.run'), \
                 patch('experiment.subprocess.check_output', return_value=''), \
                 patch('experiment.ensure_module'), patch('experiment.restore_owner'), \
                 patch('experiment.topoBuilder', return_value=network), \
                 patch('experiment.ruleInstaller') as rules, \
                 patch('experiment.SwitchScheduler') as scheduler, \
                 patch('experiment.NetworkDiagnostics') as diagnostics, \
                 patch('experiment.write_diagnostic_report') as diagnostic_report, \
                 patch('experiment.flowGenerator', return_value=timing) as generator, \
                 patch('experiment.shutil.copy2', side_effect=copy_log), \
                 patch('sys.stdout', new_callable=io.StringIO):
                scheduler.return_value.start.side_effect = lambda _: scheduler.call_args.args[1].mkdir()
                results = run(args)
            self.assertEqual([r['policy'] for r in results], ['fair', 'priority', 'priority', 'fair'])
            self.assertEqual([r['seed'] for r in results], [1729, 1729, 1730, 1730])
            self.assertTrue(all(call.kwargs['fixed_paths'] for call in rules.call_args_list))
            configs = [call.args[0] for call in scheduler.call_args_list]
            self.assertTrue(all(c.rate_mbps == 100 and c.delay_seconds == .5 for c in configs))
            self.assertEqual(diagnostics.call_count, 4)
            self.assertEqual(generator.call_count, 4)
            diagnostic_report.assert_called()
            state = json.loads((output / 'status.json').read_text())
            self.assertEqual((state['status'], state['completed_runs'], state['requested_runs']),
                             ('complete', 4, 4))
            settings = json.loads((output / 'settings.json').read_text())
            self.assertEqual(settings['priority_policy'], 'unknown_mice_first')
            self.assertEqual(settings['scheduler_policy_version'], '3')
            self.assertTrue(settings['fixed_paths'])
            self.assertTrue(settings['cdf'].endswith('DCTCP_CDF.txt'))
            self.assertTrue((output / 'qos.csv').exists())
            self.assertIn('UNKNOWN/MICE', (output / 'summary.txt').read_text())
            self.assertTrue(all((output / ('%02d_%s' % (r['repeat'], r['policy'])) /
                                 'qos.json').exists() for r in results))

    def test_redirected_progress_has_newlines_context_and_persistent_counts(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                progress = ExperimentProgress(root, 'priority', 2, 5, 3, 10)
                progress.stage('서버 및 트래픽 준비 중')
                progress.update([0, 0], 50, 35, 35)
                progress.update([20, 30], 50, 40, 0)
                progress.stage('네트워크 정리 중')
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 4)
                self.assertTrue(all('[실험 3/10 | priority 2/5회]' in line for line in lines))
                self.assertIn('no completion for 00:00:35', lines[1])
                self.assertIn('50/100 flows ( 50.0%)', lines[2])
                self.assertNotIn('\r', output.getvalue())
            saved = json.loads((root / 'progress.json').read_text())
            self.assertEqual(saved['completed_flows'], 50)
            self.assertEqual(saved['clients'], [20, 30])
            self.assertEqual(saved['phase'], '네트워크 정리 중')
            self.assertFalse((root / 'progress.json.tmp').exists())

    def test_printed_time_comes_from_transfer_timestamps_and_cleanup_always_runs(self):
        args = parse_args(['2', '3', '2', '3', '2', '5001', '2'])
        network = MagicMock()
        timing = dict(completed_flows=6, first_request_ns=1_000_000_000,
                      last_completion_ns=4_250_000_000, transfer_time_seconds=3.25)

        def copy_log(source, destination):
            destination.write_text('100 200 0 0 1\n200 300 0 0 1\n'
                                   if destination.name.startswith('flows_') else 'client log\n')

        with tempfile.TemporaryDirectory() as name:
            with patch('experiment.topoBuilder', return_value=network) as builder, \
                 patch('experiment.ruleInstaller'), \
                 patch('experiment.SwitchScheduler') as scheduler, \
                 patch('experiment.flowGenerator', return_value=timing) as generator, \
                 patch('experiment.shutil.copy2', side_effect=copy_log), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                result, sizes = run_one(args, 'legacy', 1, Path(name))
                self.assertEqual(result['transfer_time_seconds'], 3.25)
                self.assertIn('total_transfer_seconds=3.250000', output.getvalue())
                self.assertEqual(sum(sizes.values()), 6)
                self.assertFalse(generator.call_args.kwargs['capture'])
                self.assertEqual(builder.call_args.kwargs['edge_core_mbps'], 100)
                self.assertEqual(result['link_mbps'], 100)
                qos = json.loads((Path(name) / '01_legacy/qos.json').read_text())
                self.assertEqual(qos['completed_flows'], 6)
                self.assertEqual(qos['groups']['ALL']['mean_fct_ms'], .25)
                self.assertEqual(result['mean_fct_ms'], .25)
                network.stop.assert_called_once()
                network.stop.reset_mock()
                scheduler.return_value.start.side_effect = lambda _: scheduler.call_args.args[1].mkdir()
                priority, _ = run_one(args, 'priority', 1, Path(name))
                self.assertEqual(builder.call_args.kwargs['edge_core_mbps'], 100)
                config = scheduler.call_args.args[0]
                self.assertEqual(config.command(10), 'config 10 100000000 2 1514 4096 256\n')
                self.assertEqual(priority['link_mbps'], 100)
                scheduler.return_value.stop.assert_called_once()
                network.stop.reset_mock()
                timing['completed_flows'] = 5
                with self.assertRaises(RuntimeError):
                    run_one(args, 'legacy', 2, Path(name))
                network.stop.assert_called_once()

    def test_normal_links_remain_10mbps_and_experiment_override_only_changes_core_links(self):
        from topoBuilder_lb import topoBuilder
        self.assertEqual(SchedulerConfig().rate_mbps, 10)
        for kwargs, expected in (({}, 10), ({'edge_core_mbps': 100}, 100)):
            with self.subTest(kwargs=kwargs), patch('topoBuilder_lb.Mininet') as network:
                topoBuilder(2, 2, 1, 1, 1, 1, 2, **kwargs)
                topo = network.call_args.kwargs['topo']
                for a, b, info in topo.links(withInfo=True):
                    self.assertEqual(info['bw'], expected if a.startswith('core') or b.startswith('core') else 1000)

    def test_topology_arguments_and_default_comparison(self):
        args = parse_args(['3', '4', '2', '3', '2', '5001', '25'])
        self.assertEqual((args.server, args.client, args.serverEdge, args.clientEdge, args.paths),
                         (3, 4, 2, 3, 2))
        self.assertEqual(args.flowsPerClient * args.client, 100)
        self.assertEqual((args.policy, args.repeat, args.classifier_delay), ('both', 1, 0.5))

    def test_timing_uses_first_request_and_last_completion_across_clients(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'log_0.txt').write_text('FLOW_TIMING_NS 10000000000 20000000000\n')
            (root / 'log_1.txt').write_text('FLOW_TIMING_NS 12000000000 32000000000\n')
            timing = readTransferTiming(2, root)
            self.assertEqual(timing['transfer_time_seconds'], 22)
            (root / 'log_1.txt').write_text('FLOW_PROGRESS 1 2\n')
            with self.assertRaises(RuntimeError):
                readTransferTiming(2, root)

    def test_counts_are_checked_for_all_clients(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for index in range(3):
                (root / ('flows_%d.txt' % index)).write_text('100 200 0 0 1\n')
            self.assertEqual(sum(workload(root, 3, 1).values()), 3)
            (root / 'flows_2.txt').write_text('')
            with self.assertRaises(RuntimeError):
                workload(root, 3, 1)


class RoutingTests(unittest.TestCase):
    def test_arp_reaches_every_host_without_circulating(self):
        for shape in ((2, 2, 1, 1, 2), (3, 4, 2, 3, 2)):
            graph = topology_graph(*shape)
            commands = build_rules(graph)
            arp = {}
            for command in commands:
                if ' arp,' not in command:
                    continue
                name, spec = command.split()[2:4]
                incoming = int(re.search(r'in_port=(\d+)', spec).group(1))
                arp[name, incoming] = [int(p) for p in re.findall(r'output:(\d+)', spec)]
            hosts = {name for name in graph if name.startswith(('server', 'client'))}
            for start in hosts:
                seen, reached = set(), set()
                pending = [(start, next(iter(graph[start])))]
                while pending:
                    previous, node = pending.pop()
                    self.assertNotIn((previous, node), seen, 'ARP broadcast cycle')
                    seen.add((previous, node))
                    if node in hosts:
                        reached.add(node)
                        continue
                    ports = arp[node, graph[node][previous]]
                    pending.extend((node, peer) for peer, port in graph[node].items() if port in ports)
                self.assertEqual(reached, hosts - {start})

    def test_every_ipv4_ecmp_choice_reaches_the_correct_host(self):
        graph = topology_graph(12, 3, 2, 3, 2)
        groups, routes = {}, {}
        for command in build_rules(graph):
            name, spec = command.split()[2:4]
            if ' add-group ' in command:
                group = int(re.search(r'group_id=(\d+)', spec).group(1))
                groups[name, group] = [int(p) for p in re.findall(r'output:(\d+)', spec)]
            elif ' ip,' in command:
                destination = spec.split('nw_dst=')[1].split(',')[0]
                routes.setdefault(name, []).append((ipaddress.ip_network(destination, strict=False),
                                                    spec.split('actions=')[1]))
        hosts = [name for name in graph if name.startswith(('server', 'client'))]
        for source in hosts:
            for target in hosts:
                if source.startswith('server') == target.startswith('server'):
                    continue
                subnet, number = ('20.0.0.', target[6:]) if target.startswith('server') else ('20.0.1.', target[6:])
                address = ipaddress.ip_address(subnet + number)
                pending = [(next(iter(graph[source])), {source})]
                while pending:
                    node, visited = pending.pop()
                    self.assertNotIn(node, visited, 'IPv4 forwarding cycle')
                    if node in hosts:
                        self.assertEqual(node, target)
                        continue
                    choices = [(network.prefixlen, action) for network, action in routes[node]
                               if address in network]
                    self.assertTrue(choices)
                    action = max(choices)[1]
                    ports = (groups[node, int(action[6:])] if action.startswith('group:') else
                             [int(action[7:])])
                    pending.extend((peer, visited | {node}) for peer, port in graph[node].items() if port in ports)
