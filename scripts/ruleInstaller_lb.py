"""A simple rule installer to install flow/group rules for the topology built by topoBuilder_lb.py.
   Flow rules include:
        arp traffic flooding rules
        ip forwarding rules
        ip multipath routing rules (using default OVS select group / ECMP)

   Uses default OVS with standard OpenFlow13 'select' group type."""

import os

def _cmd(cmd):
    """Run an ovs-ofctl command."""
    os.system(cmd)

def ruleInstaller(network, server, client, serverEdge, clientEdge, paths):

    core = paths

    # --- Core switches ---
    for i in range(1, core + 1):
        _cmd('ovs-ofctl add-flow core%s arp,actions=flood' % i)
        for j in range(1, server + 1):
            for k in range(1, client + 1):
                _cmd('ovs-ofctl add-flow core%s ip,in_port=1,nw_src=20.0.0.%s,nw_dst=20.0.1.%s,actions=output:2' % (i, j, k))
                _cmd('ovs-ofctl add-flow core%s ip,in_port=2,nw_src=20.0.1.%s,nw_dst=20.0.0.%s,actions=output:1' % (i, k, j))

    # --- Server host-side edge switches (Hedges) ---
    for i in range(1, 2):
        _cmd('ovs-ofctl add-flow Hedges%s arp,actions=flood' % i)
        for j in range(1, server + 1):
            _cmd('ovs-ofctl add-flow Hedges%s ip,nw_dst=20.0.1.%s,actions=output:%s' % (i, j, i + server))
            _cmd('ovs-ofctl add-flow Hedges%s ip,nw_dst=20.0.0.%s,actions=output:%s' % (i, j, j))

    # --- Client host-side edge switches (Hedgec) ---
    for i in range(1, 2):
        _cmd('ovs-ofctl add-flow Hedgec%s arp,actions=flood' % i)
        for j in range(1, client + 1):
            _cmd('ovs-ofctl add-flow Hedgec%s ip,nw_dst=20.0.0.%s,actions=output:%s' % (i, j, i + client))
            _cmd('ovs-ofctl add-flow Hedgec%s ip,nw_dst=20.0.1.%s,actions=output:%s' % (i, j, j))

    # --- Server-side edge switches: downlink (Edges) ---
    for i in range(1, serverEdge + 1):
        _cmd('ovs-ofctl add-flow Edges%s arp,actions=flood' % i)
        for j in range(1, server + 1):
            _cmd('ovs-ofctl add-flow Edges%s ip,nw_dst=20.0.0.%s,actions=output:%s' % (i, j, i + paths))

    # --- Client-side edge switches: downlink (Edgec) ---
    for i in range(1, clientEdge + 1):
        _cmd('ovs-ofctl add-flow Edgec%s arp,actions=flood' % i)
        for j in range(1, client + 1):
            _cmd('ovs-ofctl add-flow Edgec%s ip,nw_dst=20.0.1.%s,actions=output:%s' % (i, j, i + paths))

    # --- Group rules: standard OVS select (ECMP, equal-weight) ---

    group_id = 1

    # Server-side edge: select group
    for i in range(1, serverEdge + 1):
        buckets = ','.join(
            'bucket=bucket_id:%s,output:%s' % (j + 1, j + 1)
            for j in range(paths)
        )
        _cmd('ovs-ofctl add-group Edges%s group_id=%s,type=select,%s' % (i, group_id, buckets))
        group_id += 1

    # Client-side edge: select group
    for i in range(1, clientEdge + 1):
        buckets = ','.join(
            'bucket=bucket_id:%s,output:%s' % (j + 1, j + 1)
            for j in range(paths)
        )
        _cmd('ovs-ofctl add-group Edgec%s group_id=%s,type=select,%s' % (i, group_id, buckets))
        group_id += 1

    # --- Flow rules that forward uplink traffic to the select group ---

    group_id = 1

    for i in range(1, serverEdge + 1):
        _cmd('ovs-ofctl add-flow Edges%s ip,in_port=%s,actions=group:%s' % (i, i + paths, group_id))
        group_id += 1

    for i in range(1, clientEdge + 1):
        _cmd('ovs-ofctl add-flow Edgec%s ip,in_port=%s,actions=group:%s' % (i, i + paths, group_id))
        group_id += 1