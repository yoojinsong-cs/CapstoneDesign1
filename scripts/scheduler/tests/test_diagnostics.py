import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from experiment import parse_args
from ruleInstaller_lb import build_rules
from scheduler.diagnostics import NetworkDiagnostics, parse_tcp_counters
from test_experiment import topology_graph


class DiagnosticTests(unittest.TestCase):
    def test_counter_parser_reads_each_network_namespace_table(self):
        text = ('Tcp: OutSegs RetransSegs CurrEstab\nTcp: 100 5 2\n'
                'TcpExt: TCPSynRetrans TCPTimeouts\nTcpExt: 1 3\n')
        self.assertEqual(parse_tcp_counters(text),
                         {'Tcp.OutSegs': 100, 'Tcp.RetransSegs': 5, 'Tcp.CurrEstab': 2,
                          'TcpExt.TCPSynRetrans': 1, 'TcpExt.TCPTimeouts': 3})

    def test_snapshots_keep_deltas_and_filter_unrelated_interfaces(self):
        host = MagicMock(name='host')
        host.name = 'server1'
        host.popen.return_value.returncode = 0
        host.popen.return_value.communicate.side_effect = [
            ('Tcp: OutSegs RetransSegs\nTcp: 100 5\n', ''),
            ('Tcp: OutSegs RetransSegs\nTcp: 200 11\n', '')]
        switch = SimpleNamespace(name='Edges1', intfList=lambda: [SimpleNamespace(name='Edges1-eth1')])
        network = SimpleNamespace(hosts=[host], switches=[switch])
        count = 0

        def command(argv, **kwargs):
            nonlocal count
            if argv[0] == 'tc':
                count += 1
                return json.dumps([dict(dev='Edges1-eth1', kind='htb', handle='5:',
                                        drops=10 if count == 1 else 13, overlimits=0),
                                   dict(dev='eth0', kind='fq_codel', handle='0:', drops=999)])
            return 'switch flow counters\n'

        with tempfile.TemporaryDirectory() as name:
            collector = NetworkDiagnostics(network, Path(name))
            before = {'Edges1-eth1': {'tx_bytes': 1000}}
            after = {'Edges1-eth1': {'tx_bytes': 5000}}
            with patch.object(collector, 'ports', side_effect=[before, before, after, after]), \
                 patch('scheduler.diagnostics.subprocess.check_output', side_effect=command):
                collector.start()
                collector.finish({'transfer_time_seconds': 2})
                collector.finish({'transfer_time_seconds': 2})
            result = json.loads((Path(name) / 'diagnostics.json').read_text())
            self.assertEqual(result['tcp']['server1']['Tcp.RetransSegs'], 6)
            self.assertEqual(result['ports']['Edges1-eth1']['tx_bytes'], 4000)
            self.assertEqual(len(result['queues']), 1)
            self.assertEqual(result['queues'][0]['drops'], 3)
            self.assertEqual(len((Path(name) / 'link_samples.jsonl').read_text().splitlines()), 2)

    def test_diagnostic_mode_is_opt_in(self):
        argv = ['2', '2', '1', '1', '2', '5001', '50']
        self.assertFalse(parse_args(argv).diagnose)
        self.assertTrue(parse_args(argv + ['--diagnose']).diagnose)
        self.assertEqual(parse_args(argv + ['--policy', 'fair']).policy, 'fair')

    def test_fixed_paths_are_loop_free_and_reproducible_in_both_directions(self):
        graph = topology_graph(3, 4, 2, 3, 2)
        rules = build_rules(graph, fixed_paths=True)
        self.assertEqual(rules, build_rules(graph, fixed_paths=True))
        forwarding = {}
        for command in rules:
            if 'priority=40000' not in command:
                continue
            node, spec = command.split()[2:4]
            source = re.search(r'nw_src=([^,]+)', spec).group(1)
            target = re.search(r'nw_dst=([^,]+)', spec).group(1)
            forwarding[node, source, target] = int(re.search(r'output:(\d+)', spec).group(1))
        for server in range(1, 4):
            for client in range(1, 5):
                for source, target, a, b in (
                        ('server%d' % server, 'client%d' % client, '20.0.0.%d' % server, '20.0.1.%d' % client),
                        ('client%d' % client, 'server%d' % server, '20.0.1.%d' % client, '20.0.0.%d' % server)):
                    node = next(iter(graph[source]))
                    visited = {source}
                    while node not in visited and node != target:
                        visited.add(node)
                        port = forwarding[node, a, b]
                        node = next(peer for peer, number in graph[node].items() if number == port)
                    self.assertEqual(node, target)
