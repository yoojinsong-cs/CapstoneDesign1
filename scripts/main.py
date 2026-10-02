import sys, os, time
from mininet.cli import CLI

from topoBuilder_lb import topoBuilder
from ruleInstaller_lb import ruleInstaller
from flowGenerator_parll import flowGenerator

if __name__ == "__main__":

    if len(sys.argv) < 8:
        print('Usage: sudo python3 %s <server#> <client#> <serverEdge#> <clientEdge#> <paths#> <port#> <flowsPerPair#>' % sys.argv[0])
        sys.exit()

    # compile traffic generator
    os.system('cd TrafficGenerator && make && cd ..')

    server       = int(sys.argv[1])
    client       = int(sys.argv[2])
    serverEdge   = int(sys.argv[3])
    clientEdge   = int(sys.argv[4])
    paths        = int(sys.argv[5])
    port         = int(sys.argv[6])
    flowsPerPair = int(sys.argv[7])

    network = topoBuilder(server, client, serverEdge, clientEdge, 1, 1, paths)
    network.start()

    ruleInstaller(network, server, client, serverEdge, clientEdge, paths)
    flowGenerator(network, port, flowsPerPair)

    time.sleep(1)

    # cleanup
    os.system('mn -c')
    os.system('pkill -KILL tshark')
    os.system('chmod 644 /tmp/client*.pcap')
