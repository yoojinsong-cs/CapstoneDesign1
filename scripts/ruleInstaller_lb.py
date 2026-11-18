"""IPv4 ECMP rules and loop-free ARP for the project's Clos topology."""

from collections import deque
import shlex
import subprocess


def _cmd(command):
    subprocess.run(shlex.split(command), check=True)


def network_neighbors(network):
    graph = {node.name: {} for node in network.hosts + network.switches}
    for link in network.links:
        a, b = link.intf1, link.intf2
        graph[a.node.name][b.node.name] = a.node.ports[a]
        graph[b.node.name][a.node.name] = b.node.ports[b]
    return graph


def fixed_path_rules(graph):
    """Pin each IP pair to the same full path across diagnostic policy runs."""
    servers = sorted(n for n in graph if n.startswith('server'))
    clients = sorted(n for n in graph if n.startswith('client'))
    server_edges = sorted(n for n in graph if n.startswith('Edges'))
    client_edges = sorted(n for n in graph if n.startswith('Edgec'))
    cores = sorted(n for n in graph if n.startswith('core'))
    commands = []
    for source in servers:
        for destination in clients:
            index = (int(source[6:]) - 1) * len(clients) + int(destination[6:]) - 1
            path = [source, 'Hedges1', server_edges[index % len(server_edges)],
                    cores[index % len(cores)], client_edges[index % len(client_edges)],
                    'Hedgec1', destination]
            src_ip, dst_ip = '20.0.0.' + source[6:], '20.0.1.' + destination[6:]
            for previous, node, following in zip(path, path[1:], path[2:]):
                for a, b, target in ((src_ip, dst_ip, following), (dst_ip, src_ip, previous)):
                    commands.append('ovs-ofctl add-flow %s priority=40000,ip,nw_src=%s,nw_dst=%s,actions=output:%d' %
                                    (node, a, b, graph[node][target]))
    return commands


def build_rules(graph, *, fixed_paths=False):
    """Resolve actual port numbers, supporting unequal host and edge counts."""
    tree = {name: set() for name in graph}
    visited = {'Hedges1'}
    pending = deque(['Hedges1'])
    while pending:
        source = pending.popleft()
        for destination in sorted(graph[source]):
            if destination in visited:
                continue
            visited.add(destination)
            tree[source].add(destination)
            tree[destination].add(source)
            pending.append(destination)
    if visited != set(graph):
        raise ValueError('topology must be connected')

    commands = []
    for name, peers in graph.items():
        if name.startswith(('server', 'client')):
            continue
        commands.append('ovs-ofctl add-flow %s priority=1,actions=drop' % name)
        for incoming, port in peers.items():
            outputs = [peers[peer] for peer in sorted(tree[name]) if peer != incoming]
            action = ','.join('output:%d' % number for number in outputs)
            if incoming not in tree[name] or not outputs:
                action = 'drop'
            commands.append('ovs-ofctl add-flow %s arp,in_port=%d,actions=%s' %
                            (name, port, action))

        group_id = 0

        def route(prefix, targets):
            nonlocal group_id
            ports = [peers[peer] for peer in sorted(peers) if peer.startswith(targets)]
            if not ports:
                raise ValueError('no next hop for %s on %s' % (prefix, name))
            if len(ports) == 1:
                action = 'output:%d' % ports[0]
            else:
                group_id += 1
                buckets = ','.join('bucket=bucket_id:%d,output:%d' % (i + 1, port)
                                   for i, port in enumerate(ports))
                commands.append('ovs-ofctl add-group %s group_id=%d,type=select,%s' %
                                (name, group_id, buckets))
                action = 'group:%d' % group_id
            commands.append('ovs-ofctl add-flow %s ip,nw_dst=%s,actions=%s' %
                            (name, prefix, action))

        if name.startswith(('Hedges', 'Hedgec')):
            host_prefix, subnet, uplinks = (('server', '20.0.0.', 'Edges')
                                            if name.startswith('Hedges') else
                                            ('client', '20.0.1.', 'Edgec'))
            for peer, port in peers.items():
                if peer.startswith(host_prefix):
                    address = subnet + str(int(peer[len(host_prefix):]))
                    commands.append('ovs-ofctl add-flow %s ip,nw_dst=%s,actions=output:%d' %
                                    (name, address, port))
            route('20.0.1.0/24' if host_prefix == 'server' else '20.0.0.0/24', uplinks)
        elif name.startswith('Edges'):
            route('20.0.0.0/24', 'Hedges')
            route('20.0.1.0/24', 'core')
        elif name.startswith('Edgec'):
            route('20.0.1.0/24', 'Hedgec')
            route('20.0.0.0/24', 'core')
        elif name.startswith('core'):
            route('20.0.0.0/24', 'Edges')
            route('20.0.1.0/24', 'Edgec')
    if fixed_paths:
        commands.extend(fixed_path_rules(graph))
    return commands


def ruleInstaller(network, server, client, serverEdge, clientEdge, paths, *, fixed_paths=False):
    for command in build_rules(network_neighbors(network), fixed_paths=fixed_paths):
        _cmd(command)
