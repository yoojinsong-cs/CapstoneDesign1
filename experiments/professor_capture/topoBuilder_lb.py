"""A simple topology builder to build a 2-stage clos network.

   serverHost --- serverEdgeSwitch --- coreSwitch --- clientEdgeSwitch --- clientHost

   Server side network is 20.0.0.0/24
   Client side network is 20.0.1.0/24

   All stages can be multipath.
   Edge <-> Core link bandwidths are fixed to 10Mbps.
   Host <-> Edge link bandwidths are fixed to 1000Mbps.

   Uses default OVS with OVSController."""

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import OVSController

def topoBuilder(server, client, serverEdge, clientEdge, serverHostEdge, clientHostEdge, paths):
    class topo(Topo):
        serverList = []
        clientList = []
        serverEdgeList = []
        clientEdgeList = []
        serverHostEdgeList = []
        clientHostEdgeList = []
        coreList = []

        def __init__(self, server, client, serverEdge, clientEdge, serverHostEdge, clientHostEdge, core):
            Topo.__init__(self)

            # Make server hosts
            for i in range(1, server + 1):
                self.serverList.append(self.addHost('server%s' % i,
                    mac='00:00:00:00:00:%s' % str(i).zfill(2),
                    ip='20.0.0.%s' % i))

            # Make client hosts
            for i in range(1, client + 1):
                self.clientList.append(self.addHost('client%s' % i,
                    mac='00:00:00:00:01:%s' % str(i).zfill(2),
                    ip='20.0.1.%s' % i))

            # Make server-side edge switches
            for i in range(1, serverEdge + 1):
                self.serverEdgeList.append(self.addSwitch('Edges%s' % i))

            # Make client-side edge switches
            for i in range(1, clientEdge + 1):
                self.clientEdgeList.append(self.addSwitch('Edgec%s' % i))

            # Make core switches
            for i in range(1, core + 1):
                self.coreList.append(self.addSwitch('core%s' % i))

            # Make server host-side switches
            for i in range(1, serverHostEdge + 1):
                self.serverHostEdgeList.append(self.addSwitch('Hedges%s' % i))

            # Make client host-side switches
            for i in range(1, clientHostEdge + 1):
                self.clientHostEdgeList.append(self.addSwitch('Hedgec%s' % i))

            # Add serverHostEdge-server links
            for i in range(serverHostEdge):
                for j in range(server):
                    self.addLink(self.serverHostEdgeList[i], self.serverList[j], bw=1000)

            # Add clientHostEdge-client links
            for i in range(clientHostEdge):
                for j in range(client):
                    self.addLink(self.clientHostEdgeList[i], self.clientList[j], bw=1000)

            baseBw = 10

            # Add serverEdge-core links
            for i in range(serverEdge):
                for j in range(core):
                    self.addLink(self.serverEdgeList[i], self.coreList[j], bw=baseBw)

            # Add clientEdge-core links
            for i in range(clientEdge):
                for j in range(core):
                    self.addLink(self.clientEdgeList[i], self.coreList[j], bw=baseBw)

            # Add serverHostEdge-serverEdge links
            for i in range(serverHostEdge):
                for j in range(serverEdge):
                    self.addLink(self.serverHostEdgeList[i], self.serverEdgeList[j], bw=1000)

            # Add clientHostEdge-clientEdge links
            for i in range(clientHostEdge):
                for j in range(clientEdge):
                    self.addLink(self.clientHostEdgeList[i], self.clientEdgeList[j], bw=1000)

    topology = topo(server=server, client=client,
                    serverEdge=serverEdge, clientEdge=clientEdge,
                    serverHostEdge=serverHostEdge, clientHostEdge=clientHostEdge,
                    core=paths)

    # 명시적인 OpenFlow 1.3과 고유 DPID. 호스트 주소/링크/개수는 원본 유지.
    for index, name in enumerate(topology.switches(), 1):
        topology.nodeInfo(name).update(dpid='%016x' % index,
                                       protocols='OpenFlow13')
    network = Mininet(topo=topology, controller=OVSController, link=TCLink,
                      build=False)
    return network
