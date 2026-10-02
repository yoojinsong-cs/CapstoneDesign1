import sys, os, time
from mininet.cli import CLI

from topoBuilder_lb import topoBuilder
from ruleInstaller_lb import ruleInstaller
from flowGenerator_parll import flowGenerator

if __name__ == "__main__":

    if len(sys.argv) < 9:
        print('Usage: sudo python3 %s <server#> <client#> <serverEdge#> <clientEdge#> <paths#> <port#> <flowsPerPair#> <clue 0..1>' % sys.argv[0])
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
    clue         = float(sys.argv[8])   # how much the app type tells about flow size (see flowGenerator_parll.py)
    assert 0 <= clue <= 1, 'clue must be between 0 and 1'

    network = topoBuilder(server, client, serverEdge, clientEdge, 1, 1, paths)
    network.start()

    ruleInstaller(network, server, client, serverEdge, clientEdge, paths)
    flowGenerator(network, port, flowsPerPair, clue)

    time.sleep(1)

    # cleanup
    os.system('mn -c')
    os.system('pkill -KILL tshark')
    os.system('chmod 644 /tmp/client*.pcap')

    # save results as data/<MMDD>_VL2_clue<NNN>_<flows>flows_client<k>.{csv,html} + _FCT.txt, owned by the sudo user
    time.sleep(1)
    for k in range(1, client + 1):
        name = 'data/%s_VL2_clue%03d_%dflows_client%d' % (time.strftime('%m%d'), round(clue * 100), flowsPerPair, k)
        os.system('python3 pcap2csv.py /tmp/client%d.pcap 10 %d > %s.csv && python3 csv2html.py %s.csv' % (k, port, name, name))
        os.system('cp flows_%d.txt %s_FCT.txt' % (k - 1, name))
        for ext in ('.csv', '.html', '_FCT.txt'):
            if os.path.exists(name + ext) and 'SUDO_UID' in os.environ:
                os.chown(name + ext, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
        print('saved', name)
