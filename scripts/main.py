import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time

from topoBuilder_lb import topoBuilder
from ruleInstaller_lb import ruleInstaller
from flowGenerator_parll import (
    FLOW_SIZE_CDF_PATH,
    flowGenerator,
    flowSizeDistributionName,
)
from scheduler.installer import SchedulerConfig, SwitchScheduler, ensure_module
from scheduler.mock_classifier import DEFAULT_CLASSIFIER_DELAY


def restoreInvokingUser(path):
    """Give sudo-created output back to the user who launched main.py."""
    sudoUid = os.environ.get('SUDO_UID')
    sudoGid = os.environ.get('SUDO_GID')
    if sudoUid is not None and sudoGid is not None:
        os.chown(path, int(sudoUid), int(sudoGid))
    os.chmod(path, 0o664)


def restoreInvokingUserDirectory(path):
    """Keep sudo-created experiment directories writable by the invoking user."""
    sudoUid = os.environ.get('SUDO_UID')
    sudoGid = os.environ.get('SUDO_GID')
    if sudoUid is not None and sudoGid is not None:
        os.chown(path, int(sudoUid), int(sudoGid))
    os.chmod(path, 0o775)


def preservePcaps(clientCount, flowsPerClient, runTag, distributionName,
                   sourceDirectory='/tmp', outputRoot='data/pcap'):
    """Preserve completed captures outside /tmp for packet diagnostics."""
    os.makedirs(outputRoot, exist_ok=True)
    restoreInvokingUserDirectory(outputRoot)
    runDirectory = os.path.join(
        outputRoot,
        'run_%s_%s_%sflows' %
        (runTag, distributionName, flowsPerClient),
    )
    os.makedirs(runDirectory, exist_ok=False)
    restoreInvokingUserDirectory(runDirectory)

    for clientIndex in range(1, clientCount + 1):
        sourcePath = os.path.join(
            sourceDirectory, 'client%s.pcap' % clientIndex)
        destinationPath = os.path.join(
            runDirectory, 'client%s.pcap' % clientIndex)
        pendingPath = destinationPath + '.pending'
        if not os.path.isfile(sourcePath):
            raise RuntimeError('missing capture: %s' % sourcePath)
        print(
            'Preserving PCAP for client%s: %s -> %s' %
            (clientIndex, sourcePath, destinationPath),
            flush=True,
        )
        try:
            shutil.copyfile(sourcePath, pendingPath)
            os.replace(pendingPath, destinationPath)
        finally:
            if os.path.exists(pendingPath):
                os.remove(pendingPath)
        restoreInvokingUser(destinationPath)

    print('PCAP preservation complete: %s' % os.path.abspath(runDirectory))
    return runDirectory


def cleanupPcapPermissions():
    for path in glob.glob('/tmp/client*.pcap'):
        try:
            os.chmod(path, 0o644)
        except OSError as exc:
            print('WARNING: cannot chmod %s: %s' % (path, exc), file=sys.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Mininet flow experiment')
    for name in ('server', 'client', 'serverEdge', 'clientEdge', 'paths',
                 'port', 'flowsPerPair'):
        parser.add_argument(name, type=int)
    parser.add_argument('clue', type=float, nargs='?', default=None,
                        help='optional app/size correlation 0..1; omitted uses bulk traffic')
    parser.add_argument('dist', nargs='?', choices=('VL2', 'DCTCP', 'FB'), default=None)
    parser.add_argument('--early-exit-seconds', type=int, default=-1,
                        help='capture-only exit delay after requests; default waits for completion')
    parser.add_argument('--scheduler', choices=('legacy', 'fifo', 'fair', 'priority'),
                        default='legacy', help='switch egress scheduler (default: legacy)')
    parser.add_argument('--classifier-delay', type=float, default=DEFAULT_CLASSIFIER_DELAY)
    parser.add_argument('--flow-threshold', type=int, default=1_000_000)
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--load-mbps', type=float, default=900,
                        help='requested offered load per client')
    parser.add_argument('--cdf', default=None)
    parser.add_argument('--run-dir', help='new directory for this experiment and FCT logs')
    args = parser.parse_args()
    if args.dist is not None and args.cdf is not None:
        parser.error('choose either dist or --cdf')
    args.cdf = args.cdf or ('TrafficGenerator/conf/%s_CDF.txt' % args.dist if args.dist else FLOW_SIZE_CDF_PATH)
    if args.clue is not None and (not math.isfinite(args.clue) or not 0 <= args.clue <= 1):
        parser.error('clue must be between 0 and 1')
    if args.early_exit_seconds < -1:
        parser.error('--early-exit-seconds must be -1 or nonnegative')
    if args.scheduler != 'legacy' and args.early_exit_seconds >= 0:
        parser.error('scheduler experiments require completed flows; omit --early-exit-seconds')
    scheduled = args.scheduler != 'legacy'
    if min(args.server, args.client, args.serverEdge, args.clientEdge, args.paths,
           args.flowsPerPair) < 1 or max(args.server, args.client) > 254 or not 1 <= args.port <= 65535:
        parser.error('host/flow counts must be positive and port must be 1..65535')
    if not math.isfinite(args.load_mbps) or args.load_mbps <= 0:
        parser.error('--load-mbps must be positive')
    if args.seed is not None and args.seed < 0:
        parser.error('--seed must be nonnegative')

    network = None
    switchScheduler = None
    exitCode = 0
    try:
        if args.run_dir and os.path.exists(args.run_dir):
            raise RuntimeError('--run-dir must be a new directory: %s' % args.run_dir)
        # Stop immediately if compilation fails; running an old binary would
        # make the source and the observed behavior disagree.
        subprocess.run(['make'], cwd='TrafficGenerator', check=True)

        server, client = args.server, args.client
        serverEdge, clientEdge, paths = args.serverEdge, args.clientEdge, args.paths
        port, flowsPerPair = args.port, args.flowsPerPair
        if scheduled:
            config = SchedulerConfig(mode=args.scheduler,
                                     delay_seconds=args.classifier_delay,
                                     threshold_bytes=args.flow_threshold)
            ensure_module(port)

        print(
            'requested traffic: %d flows/client x %d clients = %d flows' %
            (flowsPerPair, client, flowsPerPair * client),
            flush=True,
        )

        network = topoBuilder(
            server, client, serverEdge, clientEdge, 1, 1, paths)
        network.start()
        ruleInstaller(
            network, server, client, serverEdge, clientEdge, paths)
        if scheduled:
            schedulerDirectory = args.run_dir or os.path.join(
                'data', 'scheduler', '%s_%s_%s' %
                (time.strftime('%Y%m%d_%H%M%S'), args.scheduler, os.getpid()))
            switchScheduler = SwitchScheduler(config, schedulerDirectory,
                                              experiment=vars(args))
            switchScheduler.start(network)
        elif args.run_dir:
            os.makedirs(args.run_dir, exist_ok=False)
            with open(os.path.join(args.run_dir, 'manifest.json'), 'w') as manifest:
                json.dump(dict(config=dict(mode='legacy', threshold_bytes=args.flow_threshold,
                                           delay_seconds=None), experiment=vars(args)),
                          manifest, indent=2)
        flowGenerator(
            network, port, flowsPerPair, cdfPath=args.cdf,
            baseSeed=args.seed, loadMbps=args.load_mbps,
            healthCheck=switchScheduler.check if switchScheduler else None,
            clue=args.clue, earlyExitSec=args.early_exit_seconds)
        if switchScheduler:
            switchScheduler.check()
        resultDirectory = schedulerDirectory if scheduled else args.run_dir
        if resultDirectory:
            for index in range(client):
                for source in ('flows_%s.txt' % index, 'log_%s.txt' % index):
                    if source.startswith('flows_') and args.early_exit_seconds >= 0:
                        continue
                    shutil.copy2(source, os.path.join(resultDirectory, source))
            for switch in network.switches:
                snapshot = subprocess.check_output(
                    ['ovs-ofctl', 'dump-flows', switch.name], text=True)
                with open(os.path.join(resultDirectory, 'ovs_%s.txt' % switch.name), 'w') as output:
                    output.write(snapshot)
        if switchScheduler:
            switchScheduler.stop()
        runTag = time.strftime('%Y%m%d_%H%M%S')
        distributionName = flowSizeDistributionName(args.cdf)
        preservePcaps(
            client,
            flowsPerPair,
            runTag,
            distributionName,
        )
        time.sleep(1)
    except KeyboardInterrupt:
        print('\nInterrupted by user; cleaning up Mininet...')
        exitCode = 130
    except Exception as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        exitCode = 1
    finally:
        if switchScheduler is not None:
            try:
                switchScheduler.stop()
            except Exception as exc:
                print('WARNING: scheduler cleanup failed: %s' % exc, file=sys.stderr)
        if args.run_dir and os.path.isdir(args.run_dir):
            restoreInvokingUserDirectory(args.run_dir)
            for path in glob.glob(os.path.join(args.run_dir, '*')):
                if os.path.isfile(path):
                    restoreInvokingUser(path)
        if network is not None:
            try:
                network.stop()
            except Exception as exc:
                print('WARNING: network.stop() failed: %s' % exc, file=sys.stderr)
        if network is not None:
            try:
                subprocess.run(['mn', '-c'], check=False)
            except OSError as exc:
                print('WARNING: Mininet cleanup failed: %s' % exc, file=sys.stderr)
        cleanupPcapPermissions()

    sys.exit(exitCode)
