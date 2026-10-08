"""Generate flows using the TrafficGenerator tool.
   https://github.com/HKUST-SING/TrafficGenerator """

import multiprocessing
import os
import re
import time


MAX_CONNECTIONS_PER_SERVER = 256
PROGRESS_RE = re.compile(r'^FLOW_PROGRESS\s+(\d+)\s+(\d+)$')
EXIT_CODE_RE = re.compile(r'^CLIENT_EXIT_CODE\s+(\d+)$')


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
    if finished and idleFor >= 30:
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
            host.cmd('kill -%s %s 2>/dev/null || true' % (signalName, pid))
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


def printIdealRuntimeEstimate(network, clientCount, flowsPerPair):
    cores = sum(1 for switch in network.switches if switch.name.startswith('core'))
    serverEdges = sum(1 for switch in network.switches if switch.name.startswith('Edges'))
    clientEdges = sum(1 for switch in network.switches if switch.name.startswith('Edgec'))
    # topoBuilder_lb.py fixes every edge/core link at 10 Mbps.
    idealMbps = cores * min(serverEdges, clientEdges) * 10
    if idealMbps <= 0:
        return
    averageBytes = averageCdfBytes('TrafficGenerator/conf/DCTCP_CDF.txt')
    expectedBytes = averageBytes * clientCount * flowsPerPair
    idealSeconds = expectedBytes * 8 / (idealMbps * 1000000.0)
    print('expected data: %.2f GB; ideal %s Mbps transfer lower bound: %s' %
          (expectedBytes / 1000000000.0, idealMbps,
           formatDuration(idealSeconds)))


def flowGenerator(network, port, flowsPerPair):
    allHosts = network.hosts
    serverHosts = [host for host in allHosts if host.name.startswith('server')]
    clientHosts = [host for host in allHosts if host.name.startswith('client')]

    if not serverHosts or not clientHosts:
        raise RuntimeError('topology must contain at least one server and one client')

    for clientIndex in range(len(clientHosts)):
        serverIp = serverHosts[clientIndex % len(serverHosts)].params['ip']
        configLines = [
            'server %s %s' % (serverIp, port),
            'req_size_dist TrafficGenerator/conf/DCTCP_CDF.txt',
        ]
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

        printIdealRuntimeEstimate(network, len(clientHosts), flowsPerPair)

        # /tmp is writable after dumpcap drops root privileges.  A 128-byte
        # snapshot retains headers and request payload while limiting disk use.
        for clientHost in clientHosts:
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

        for clientHost in clientHosts:
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
        time.sleep(1)

        baseSeed = int(time.time()) % 1000000
        print('baseSeed', baseSeed)
        print('clients', len(clientHosts))
        print('connection limit: %s per client/server pair' % MAX_CONNECTIONS_PER_SERVER)

        for clientIndex, clientHost in enumerate(clientHosts):
            logPath = 'log_%s.txt' % clientIndex
            if os.path.exists(logPath):
                os.remove(logPath)
            seed = baseSeed * 100 + clientIndex + 1
            print('start flows from %s (seed %s)' % (clientHost.name, seed))
            process = multiprocessing.Process(
                target=startClient,
                args=(clientHosts, flowsPerPair, clientIndex, seed))
            process.start()
            clientProcesses.append(process)

        startedAt = time.monotonic()
        lastProgressAt = startedAt
        previousFinished = 0
        previousLength = 0
        progress = [0] * len(clientHosts)
        progressOffsets = [0] * len(clientHosts)

        while any(process.is_alive() for process in clientProcesses):
            now = time.monotonic()
            updateClientProgress(progress, progressOffsets, flowsPerPair)
            finished = sum(progress)
            if finished != previousFinished:
                previousFinished = finished
                lastProgressAt = now
            status = formatProgress(
                progress, flowsPerPair, now - startedAt, now - lastProgressAt)
            print('\r' + status.ljust(previousLength), end='', flush=True)
            previousLength = max(previousLength, len(status))
            time.sleep(2)

        for process in clientProcesses:
            process.join()

        now = time.monotonic()
        updateClientProgress(progress, progressOffsets, flowsPerPair)
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
        if failedClients or incompleteClients:
            raise RuntimeError(
                'traffic generation failed; failed clients=%s, unfinished clients=%s. '
                'See log_N.txt for the exact socket/thread error.' %
                (failedClients, incompleteClients))

        print('client requests done...')
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


def startClient(clientHosts, flowsPerPair, clientIndex, seed):
    logPath = 'log_%s.txt' % clientIndex
    command = (
        'TrafficGenerator/bin/client -b 900 '
        '-c TrafficGenerator/conf/client%s_config.txt -n %s '
        '-l flows_%s.txt -s %s -m %s -v > %s 2>&1; '
        'client_rc=$?; echo CLIENT_EXIT_CODE $client_rc >> %s' %
        (clientIndex, flowsPerPair, clientIndex, seed,
         MAX_CONNECTIONS_PER_SERVER, logPath, logPath))
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
