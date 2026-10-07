"""교수님 flowGenerator의 배정/CDF/부하/seed를 유지하고 로그·프로세스를 분리."""

from contextlib import ExitStack
import shlex
import shutil
import signal
import subprocess
import time


def stop(process, sig=signal.SIGTERM):
    if process is not None and process.poll() is None:
        process.send_signal(sig)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def flowGenerator(network, port, flowsPerPair, out, generator, timeout=1200):
    servers = [h for h in network.hosts if h.name.startswith('s')]
    clients = [h for h in network.hosts if h.name.startswith('c')]
    server_processes, client_processes, pairs = [], [], []
    # 원본 client는 config/CDF/log 경로를 80바이트 배열에 보관한다.
    # 실행 폴더를 out으로 정하고 짧은 파일명을 사용한다.
    shutil.copyfile(generator / 'conf/DCTCP_CDF.txt', out / 'DCTCP_CDF.txt')
    with ExitStack() as files:
        try:
            for server in servers:
                log = files.enter_context((out / (server.name + '.log')).open('w'))
                # -d 대신 foreground 자식을 보존하여 이번 서버 PID만 정리한다.
                proc = server.popen([str(generator / 'bin/server'), '-p', str(port), '-v'],
                                    stdout=log, stderr=subprocess.STDOUT)
                server_processes.append(proc)
                for _ in range(50):
                    if proc.poll() is not None:
                        raise RuntimeError(server.name + ' exited before LISTEN')
                    result, _, code = server.pexec(['ss', '-H', '-ltn', 'sport = :' + str(port)])
                    if not code and result.strip():
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError(server.name + ' LISTEN timeout')

            for index, client in enumerate(clients):
                server = servers[index % len(servers)]
                config = out / (client.name + '.conf')
                config.write_text('server %s %s\nreq_size_dist DCTCP_CDF.txt\n' %
                                  (server.IP(), port))
                flow_log = out / (client.name + '-flows.txt')
                command = [str(generator / 'bin/client'), '-b', '900', '-c', config.name,
                           '-n', str(flowsPerPair), '-l', flow_log.name, '-s', '1', '-v']
                (out / (client.name + '-command.txt')).write_text('cwd: ' + str(out) + '\n' + shlex.join(command) + '\n')
                log = files.enter_context((out / (client.name + '.log')).open('w'))
                proc = client.popen(['stdbuf', '-oL', *command], stdout=log,
                                    stderr=subprocess.STDOUT, cwd=out)
                client_processes.append(proc)
                pairs.append(dict(client=client.name, server=server.name,
                                  client_ip=client.IP(), server_ip=server.IP(),
                                  requests=flowsPerPair, flows_file=flow_log.name))

            deadline = time.monotonic() + timeout
            while any(p.poll() is None for p in client_processes):
                if time.monotonic() > deadline:
                    raise TimeoutError('TrafficGenerator exceeded %s seconds' % timeout)
                time.sleep(.5)
            if any(p.returncode != 0 for p in client_processes):
                raise RuntimeError('TrafficGenerator client failed; see client logs')
            for pair in pairs:
                rows = (out / pair['flows_file']).read_text().splitlines()
                if len(rows) != flowsPerPair or any(len(row.split()) != 5 for row in rows):
                    raise RuntimeError('Incomplete flow log: ' + pair['flows_file'])
            return pairs
        finally:
            for proc in client_processes + server_processes:
                stop(proc)
