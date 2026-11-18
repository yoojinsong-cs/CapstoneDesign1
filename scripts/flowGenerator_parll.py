"""Generate flows using the TrafficGenerator tool.
   https://github.com/HKUST-SING/TrafficGenerator """

import math
import multiprocessing
import os
import re
import time


MAX_CONNECTIONS_PER_SERVER = 256
FLOW_SIZE_CDF_PATH = 'TrafficGenerator/conf/VL2_CDF.txt'
PROGRESS_RE = re.compile(r'^FLOW_PROGRESS\s+(\d+)\s+(\d+)$')
EXIT_CODE_RE = re.compile(r'^CLIENT_EXIT_CODE\s+(\d+)$')
TIMING_RE = re.compile(r'^FLOW_TIMING_NS\s+(\d+)\s+(\d+)$')


def readTransferTiming(clientCount, directory='.'):
    """First request to last completed flow, on the shared host monotonic clock."""
    timings = []
    for index in range(clientCount):
        timing = None
        with open(os.path.join(directory, 'log_%d.txt' % index)) as stream:
            for line in stream:
                match = TIMING_RE.match(line.strip())
                if match:
                    timing = tuple(map(int, match.groups()))
        if timing is None or not 0 < timing[0] <= timing[1]:
            raise RuntimeError('missing or invalid completed transfer timing for client%d' % (index + 1))
        timings.append(timing)
    first = min(start for start, _ in timings)
    last = max(end for _, end in timings)
    return dict(first_request_ns=first, last_completion_ns=last,
                transfer_time_seconds=(last - first) / 1_000_000_000)


def flowSizeDistributionName(cdfPath=FLOW_SIZE_CDF_PATH):
    """Return the distribution label used in generated artifact names."""
    name = os.path.splitext(os.path.basename(cdfPath))[0]
    if name.upper().endswith('_CDF'):
        name = name[:-4]
    if not name:
        raise ValueError('cannot derive distribution name from %r' % cdfPath)
    return name


def updateClientProgress(progress, offsets, flowsPerPair):
    """Read only newly appended progress records from each client log."""
    for clientIndex in range(len(progress)):
        logPath = 'log_%s.txt' % clientIndex
        try:
            with open(logPath, 'r', errors='replace') as logFile:
                logFile.seek(offsets[clientIndex])
                for line in logFile:
                    match = PROGRESS_RE.match(line.strip())
                    if match:
                        progress[clientIndex] = min(int(match.group(1)), flowsPerPair)
                offsets[clientIndex] = logFile.tell()
        except OSError:
            pass
    return progress


def formatDuration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return '%02d:%02d:%02d' % (hours, minutes, seconds)


def formatProgress(progress, flowsPerPair, elapsed, idleFor):
    total = len(progress) * flowsPerPair
    finished = sum(progress)
    percent = (finished * 100.0 / total) if total else 100.0
    clients = ' | '.join(
        'client%s %s/%s' % (index + 1, count, flowsPerPair)
        for index, count in enumerate(progress)
    )
    status = ('[progress %s] %s/%s flows (%5.1f%%) | %s' %
              (formatDuration(elapsed), finished, total, percent, clients))
    if idleFor >= 30:
        status += ' | no completion for %s' % formatDuration(idleFor)
    return status


def parseBackgroundPid(output, processName):
    matches = re.findall(r'\b\d+\b', output or '')
    if not matches:
        raise RuntimeError('could not obtain %s PID: %r' % (processName, output))
    return int(matches[-1])


def stopManagedProcesses(entries, signalName):
    for host, pid in entries:
        try:
            # A client can still occupy this node's interactive shell after its
            # Python wrapper is interrupted. Use an independent process so
            # capture/server cleanup never waits for that shell to become idle.
            process = host.popen(['kill', '-%s' % signalName, str(pid)])
            process.wait(timeout=5)
        except Exception as exc:
            print('WARNING: could not stop PID %s on %s: %s' % (pid, host.name, exc))


def averageCdfBytes(path):
    entries = []
    with open(path, 'r') as cdfFile:
        for line in cdfFile:
            fields = line.split()
            if len(fields) >= 2:
                entries.append((float(fields[0]), float(fields[1])))
    average = 0.0
    previousValue = 0.0
    previousCdf = 0.0
    for value, cdf in entries:
        average += ((value + previousValue) / 2.0) * (cdf - previousCdf)
        previousValue = value
        previousCdf = cdf
    return average


def printIdealRuntimeEstimate(network, clientCount, flowsPerPair,
                              cdfPath=FLOW_SIZE_CDF_PATH):
    cores = sum(1 for switch in network.switches if switch.name.startswith('core'))
    serverEdges = sum(1 for switch in network.switches if switch.name.startswith('Edges'))
    clientEdges = sum(1 for switch in network.switches if switch.name.startswith('Edgec'))
    edgeCoreMbps = getattr(network, 'edge_core_mbps', 10)
    idealMbps = cores * min(serverEdges, clientEdges) * edgeCoreMbps
    if idealMbps <= 0:
        return
    averageBytes = averageCdfBytes(cdfPath)
    expectedBytes = averageBytes * clientCount * flowsPerPair
    idealSeconds = expectedBytes * 8 / (idealMbps * 1000000.0)
    print('expected data: %.2f GB; ideal %s Mbps transfer lower bound: %s' %
          (expectedBytes / 1000000000.0, idealMbps,
           formatDuration(idealSeconds)))


# app type by flow size, written as client config lines: app_mix <max_size> <w_bulk> <w_login> <w_stream>
# clue = how much the app type tells about the size (0..1):
#   0 -> every size bucket gets the same mix (app type says nothing about size)
#   1 -> small = login, medium = bulk, large (>= elephant) = stream (app type gives the answer away)
SIZE_BUCKETS = [10000, 1000000, 4294967295]   # small <= 10 KB < medium <= 1 MB < large
NEUTRAL = [1, 1, 1]                           # bulk, login, stream
STRONG = [[0, 1, 0], [1, 0, 0], [0, 0, 1]]    # per bucket

def appMixLines (clue):
    lines = []
    for bound, strong in zip(SIZE_BUCKETS, STRONG):
        w = [round(1000 * ((1 - clue) * n / sum(NEUTRAL) + clue * s)) for n, s in zip(NEUTRAL, strong)]
        lines.append('app_mix %d %d %d %d' % (bound, w[0], w[1], w[2]))
    return lines


def flowGenerator(network, port, flowsPerPair,
                  cdfPath=FLOW_SIZE_CDF_PATH, baseSeed=None, loadMbps=900,
                  healthCheck=None, capture=True, progressCallback=None,
                  clue=None, dist=None, earlyExitSec=-1):
    if clue is not None and (not math.isfinite(clue) or not 0 <= clue <= 1):
        raise ValueError('clue must be between 0 and 1')
    if not isinstance(earlyExitSec, int) or earlyExitSec < -1:
        raise ValueError('earlyExitSec must be -1 or a nonnegative integer')
    if dist is not None:
        if cdfPath != FLOW_SIZE_CDF_PATH:
            raise ValueError('choose either dist or cdfPath')
        if dist not in ('VL2', 'DCTCP', 'FB'):
            raise ValueError('unknown flow size distribution')
        cdfPath = 'TrafficGenerator/conf/%s_CDF.txt' % dist
    if not os.path.isfile(cdfPath):
        raise ValueError('CDF file does not exist: %s' % cdfPath)
    allHosts = network.hosts
    serverHosts = [host for host in allHosts if host.name.startswith('server')]
    clientHosts = [host for host in allHosts if host.name.startswith('client')]

    if not serverHosts or not clientHosts:
        raise RuntimeError('topology must contain at least one server and one client')

    for clientIndex in range(len(clientHosts)):
        serverIp = serverHosts[clientIndex % len(serverHosts)].params['ip']
        configLines = [
            'server %s %s' % (serverIp, port),
            'req_size_dist %s' % cdfPath,
        ]
        if clue is not None:
            configLines.extend(appMixLines(clue))
        with open('TrafficGenerator/conf/client%s_config.txt' % clientIndex, 'w') as configFile:
            configFile.write('\n'.join(configLines) + '\n')

    resultText = 'flows.txt'
    if os.path.exists('TrafficGenerator/%s' % resultText):
        os.remove('TrafficGenerator/%s' % resultText)

    # Capture what is on the wire (1448-byte segments), not GRO/GSO aggregates.
    for node in network.hosts + network.switches:
        for intf in node.intfNames():
            if intf != 'lo':
                node.cmd('ethtool -K %s gro off gso off tso off' % intf)

    serverProcesses = startServer(serverHosts, port)
    captureProcesses = []
    clientProcesses = []

    try:
        time.sleep(1)
        print('opened all servers...')
        for serverHost, serverPid in serverProcesses:
            alive = serverHost.cmd(
                'kill -0 %s 2>/dev/null; echo $?' % serverPid).strip().splitlines()
            if not alive or alive[-1] != '0':
                raise RuntimeError(
                    'server on %s exited during startup; see server_%s.log' %
                    (serverHost.name, serverHost.name))

        print('flow size distribution: %s (%s)' %
              (flowSizeDistributionName(cdfPath), cdfPath))
        printIdealRuntimeEstimate(
            network, len(clientHosts), flowsPerPair, cdfPath)

        # /tmp is writable after dumpcap drops root privileges.  A 128-byte
        # snapshot retains headers and request payload while limiting disk use.
        for clientHost in (clientHosts if capture else []):
            captureLog = 'tshark_%s.log' % clientHost.name
            capturePath = '/tmp/%s.pcap' % clientHost.name
            if os.path.exists(captureLog):
                os.remove(captureLog)
            if os.path.exists(capturePath):
                os.remove(capturePath)
            output = clientHost.cmd(
                'tshark -s 128 -i %s-eth0 -f "tcp port %s" '
                '-w /tmp/%s.pcap > %s 2>&1 & echo $!' %
                (clientHost.name, port, clientHost.name, captureLog))
            captureProcesses.append(
                (clientHost, parseBackgroundPid(output, 'tshark on %s' % clientHost.name)))

        for clientHost in (clientHosts if capture else []):
            captureLog = 'tshark_%s.log' % clientHost.name
            for _ in range(60):
                try:
                    with open(captureLog, 'r', errors='replace') as logFile:
                        if 'Capturing on' in logFile.read():
                            break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError('tshark on %s did not start' % clientHost.name)
        if capture:
            time.sleep(1)

        if baseSeed is None:
            baseSeed = int(time.time()) % 1000000
        print('baseSeed', baseSeed)
        print('clients', len(clientHosts))
        print('connection limit: %s per client/server pair' % MAX_CONNECTIONS_PER_SERVER)

        startedAt = time.monotonic()
        for clientIndex, clientHost in enumerate(clientHosts):
            logPath = 'log_%s.txt' % clientIndex
            if os.path.exists(logPath):
                os.remove(logPath)
            flowLog = 'flows_%s.txt' % clientIndex
            if os.path.exists(flowLog):
                os.remove(flowLog)
            seed = baseSeed * 100 + clientIndex + 1
            print('start flows from %s (seed %s)' % (clientHost.name, seed))
            process = multiprocessing.Process(
                target=startClient,
                args=(clientHosts, flowsPerPair, clientIndex, seed, loadMbps, earlyExitSec))
            process.start()
            clientProcesses.append(process)

        lastProgressAt = startedAt
        previousFinished = 0
        previousLength = 0
        progress = [0] * len(clientHosts)
        progressOffsets = [0] * len(clientHosts)

        while any(process.is_alive() for process in clientProcesses):
            if healthCheck is not None:
                healthCheck()
            now = time.monotonic()
            updateClientProgress(progress, progressOffsets, flowsPerPair)
            finished = sum(progress)
            if finished != previousFinished:
                previousFinished = finished
                lastProgressAt = now
            if progressCallback is not None:
                progressCallback(list(progress), flowsPerPair, now - startedAt, now - lastProgressAt)
            else:
                status = formatProgress(
                    progress, flowsPerPair, now - startedAt, now - lastProgressAt)
                print('\r' + status.ljust(previousLength), end='', flush=True)
                previousLength = max(previousLength, len(status))
            time.sleep(2)

        for process in clientProcesses:
            process.join()

        now = time.monotonic()
        updateClientProgress(progress, progressOffsets, flowsPerPair)
        if progressCallback is not None:
            progressCallback(list(progress), flowsPerPair, now - startedAt, now - lastProgressAt)
        else:
            status = formatProgress(
                progress, flowsPerPair, now - startedAt, now - lastProgressAt)
            print('\r' + status.ljust(previousLength), flush=True)

        failedClients = [
            index + 1 for index, process in enumerate(clientProcesses)
            if process.exitcode != 0
        ]
        incompleteClients = [
            index + 1 for index, count in enumerate(progress)
            if count < flowsPerPair
        ]
        if failedClients or (earlyExitSec < 0 and incompleteClients):
            raise RuntimeError(
                'traffic generation failed; failed clients=%s, unfinished clients=%s. '
                'See log_N.txt for the exact socket/thread error.' %
                (failedClients, incompleteClients))

        print('client requests done...')
        if earlyExitSec >= 0:
            return dict(completed_flows=sum(progress), early_exit_seconds=earlyExitSec)
        timing = readTransferTiming(len(clientHosts))
        timing['completed_flows'] = sum(progress)
        return timing
    except KeyboardInterrupt:
        print('\nInterrupted; stopping clients, captures, and servers...')
        raise
    finally:
        for process in clientProcesses:
            if process.is_alive():
                process.terminate()
        for process in clientProcesses:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join()

        # SIGINT lets tshark flush a valid pcap trailer.
        stopManagedProcesses(captureProcesses, 'INT')
        if captureProcesses:
            time.sleep(0.5)
        stopManagedProcesses(serverProcesses, 'TERM')


def startServer(serverHosts, port):
    processes = []
    try:
        for server in serverHosts:
            print('open %s port#%s' % (server.name, port))
            logPath = 'server_%s.log' % server.name
            output = server.cmd(
                'TrafficGenerator/bin/server -p %s > %s 2>&1 & echo $!' %
                (port, logPath))
            processes.append(
                (server, parseBackgroundPid(output, 'server on %s' % server.name)))
    except Exception:
        stopManagedProcesses(processes, 'TERM')
        raise
    return processes


def startClient(clientHosts, flowsPerPair, clientIndex, seed, loadMbps=900, earlyExitSec=-1):
    logPath = 'log_%s.txt' % clientIndex
    command = (
        'TrafficGenerator/bin/client -b %s '
        '-c TrafficGenerator/conf/client%s_config.txt -n %s '
        '-l flows_%s.txt -s %s -m %s %s -v > %s 2>&1; '
        'client_rc=$?; echo CLIENT_EXIT_CODE $client_rc >> %s' %
        (loadMbps, clientIndex, flowsPerPair, clientIndex, seed,
         MAX_CONNECTIONS_PER_SERVER, '-e %d' % earlyExitSec if earlyExitSec >= 0 else '',
         logPath, logPath))
    clientHosts[clientIndex].cmd(command)

    exitCode = 1
    try:
        with open(logPath, 'r', errors='replace') as logFile:
            for line in logFile:
                match = EXIT_CODE_RE.match(line.strip())
                if match:
                    exitCode = int(match.group(1))
    except OSError:
        pass
    raise SystemExit(exitCode)
