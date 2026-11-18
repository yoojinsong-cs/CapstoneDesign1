"""Read-only per-experiment queue, path, and TCP measurements."""

import json
from pathlib import Path
import subprocess
import time


def parse_tcp_counters(text):
    lines = text.splitlines()
    result = {}
    for first, second in zip(lines, lines[1:]):
        names, values = first.split(), second.split()
        if names and values and names[0] == values[0] and len(names) == len(values):
            for key, value in zip(names[1:], values[1:]):
                if value.lstrip('-').isdigit():
                    result[names[0].rstrip(':') + '.' + key] = int(value)
    return result


def counter_delta(before, after):
    return {key: value - before.get(key, 0) for key, value in after.items()}


class NetworkDiagnostics:
    def __init__(self, network, directory):
        self.network = network
        self.directory = Path(directory)
        self.interfaces = {interface.name for switch in network.switches
                           for interface in switch.intfList() if interface.name != 'lo'}
        self.before = None
        self.finished = False
        self.last_sample = 0

    def ports(self):
        result = {}
        for name in sorted(self.interfaces):
            base = Path('/sys/class/net') / name / 'statistics'
            result[name] = {key: int((base / key).read_text())
                            for key in ('tx_bytes', 'tx_packets', 'tx_dropped',
                                        'rx_bytes', 'rx_packets', 'rx_dropped')}
        return result

    def snapshot(self, label):
        queues = json.loads(subprocess.check_output(['tc', '-j', '-s', 'qdisc', 'show'], text=True))
        queues = [q for q in queues if q.get('dev') in self.interfaces]
        tcp = {}
        for host in self.network.hosts:
            process = host.popen(['cat', '/proc/net/snmp', '/proc/net/netstat'],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                text, error = process.communicate(timeout=5)
            except BaseException:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                raise RuntimeError('TCP counter collection failed on %s: %s' % (host.name, error))
            tcp[host.name] = parse_tcp_counters(text)
        snapshot = dict(time_ns=time.monotonic_ns(), ports=self.ports(), queues=queues, tcp=tcp)
        (self.directory / ('network_%s.json' % label)).write_text(json.dumps(snapshot, indent=2) + '\n')
        for switch in self.network.switches:
            output = subprocess.check_output(['ovs-ofctl', 'dump-flows', switch.name], text=True)
            (self.directory / ('ovs_%s_%s.txt' % (label, switch.name))).write_text(output)
        return snapshot

    def start(self):
        self.before = self.snapshot('before')
        self.sample(force=True)

    def sample(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_sample < 2:
            return
        self.last_sample = now
        with (self.directory / 'link_samples.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(time_ns=time.monotonic_ns(), ports=self.ports())) + '\n')

    def finish(self, timing=None):
        if self.finished or self.before is None:
            return
        after = self.snapshot('after')
        self.sample(force=True)
        port_delta = {name: counter_delta(self.before['ports'][name], values)
                      for name, values in after['ports'].items()}
        tcp_delta = {name: counter_delta(self.before['tcp'][name], values)
                     for name, values in after['tcp'].items()}
        queue_delta = []
        for queue in after['queues']:
            previous = next((q for q in self.before['queues'] if
                             (q['dev'], q.get('handle')) == (queue['dev'], queue.get('handle'))), {})
            queue_delta.append(dict(interface=queue['dev'], kind=queue['kind'],
                                    handle=queue.get('handle'),
                                    drops=queue.get('drops', 0) - previous.get('drops', 0),
                                    overlimits=queue.get('overlimits', 0) - previous.get('overlimits', 0)))
        summary = dict(ports=port_delta, tcp=tcp_delta, queues=queue_delta,
                       completed=timing is not None)
        if timing:
            summary['transfer_time_seconds'] = timing['transfer_time_seconds']
        (self.directory / 'diagnostics.json').write_text(json.dumps(summary, indent=2) + '\n')
        self.finished = True


def write_diagnostic_report(output, results):
    lines = ['원인 구분용 통제 실험',
             '모든 정책에 동일한 IP 쌍별 경로를 고정한다.']
    policies = {r['policy'] for r in results}
    for pair, description in (
            (('legacy', 'fifo'), '기존 HTB와 커스텀 큐/속도제어 차이'),
            (('fifo', 'fair'), '같은 속도제어에서 flow별 공평 분배 차이'),
            (('fair', 'priority'), '같은 큐 한도/속도제어에서 UNKNOWN/MICE 우선순위 차이')):
        if set(pair) <= policies:
            lines.append('%s↔%s: %s' % (*pair, description))
    lines.extend(['', '회차 정책 전송시간(초) 유효속도(Mbps) 서버TCP재전송수 큐drop수(위치별)'])
    for result in results:
        folder = Path(output) / ('%02d_%s' % (result['repeat'], result['policy']))
        details = json.loads((folder / 'diagnostics.json').read_text())
        retrans = sum(c.get('Tcp.RetransSegs', 0) for name, c in details['tcp'].items()
                      if name.startswith('server'))
        drops = ', '.join('%s/%s:%d' % (q['interface'], q['handle'], q['drops'])
                          for q in details['queues'] if q['drops']) or '0'
        rate = result['total_payload_bytes'] * 8 / max(result['transfer_time_seconds'], 1e-9) / 1e6
        lines.append('%d %s %.6f %.3f %d %s' %
                     (result['repeat'], result['policy'], result['transfer_time_seconds'], rate, retrans, drops))
        ports = details['ports']
        for name in sorted(ports):
            if name.startswith('Edges'):
                lines.append('  %s: 출력 %.3fMB' % (name, ports[name]['tx_bytes'] / 1e6))
    lines.extend(['', '이전 실험에는 legacy 재전송·큐 통계가 없어 당시 차이의 원인을 소급 확정할 수 없다.',
                  '새 통제 실험의 시간 차이와 재전송/drop/경로 기록을 함께 비교해야 한다.'])
    (Path(output) / 'diagnosis.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
