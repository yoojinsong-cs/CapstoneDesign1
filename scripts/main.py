import glob
import os
import subprocess
import sys
import time

from topoBuilder_lb import topoBuilder
from ruleInstaller_lb import ruleInstaller
from flowGenerator_parll import flowGenerator


def cleanupPcapPermissions():
    for path in glob.glob('/tmp/client*.pcap'):
        try:
            os.chmod(path, 0o644)
        except OSError as exc:
            print('WARNING: cannot chmod %s: %s' % (path, exc), file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) < 8:
        print('Usage: sudo python3 %s <server#> <client#> <serverEdge#> '
              '<clientEdge#> <paths#> <port#> <flowsPerPair#>' % sys.argv[0])
        sys.exit(2)

    network = None
    exitCode = 0
    try:
        # Stop immediately if compilation fails; running an old binary would
        # make the source and the observed behavior disagree.
        subprocess.run(['make'], cwd='TrafficGenerator', check=True)

        server = int(sys.argv[1])
        client = int(sys.argv[2])
        serverEdge = int(sys.argv[3])
        clientEdge = int(sys.argv[4])
        paths = int(sys.argv[5])
        port = int(sys.argv[6])
        flowsPerPair = int(sys.argv[7])

        network = topoBuilder(
            server, client, serverEdge, clientEdge, 1, 1, paths)
        network.start()
        ruleInstaller(
            network, server, client, serverEdge, clientEdge, paths)
        flowGenerator(network, port, flowsPerPair)
        time.sleep(1)
    except KeyboardInterrupt:
        print('\nInterrupted by user; cleaning up Mininet...')
        exitCode = 130
    except Exception as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        exitCode = 1
    finally:
        if network is not None:
            try:
                network.stop()
            except Exception as exc:
                print('WARNING: network.stop() failed: %s' % exc, file=sys.stderr)
        try:
            subprocess.run(['mn', '-c'], check=False)
        except OSError as exc:
            print('WARNING: Mininet cleanup failed: %s' % exc, file=sys.stderr)
        cleanupPcapPermissions()

    sys.exit(exitCode)
