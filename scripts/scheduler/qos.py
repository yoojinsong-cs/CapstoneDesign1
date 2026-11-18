"""Receiver-observed application QoS, calculated from completed client logs.

Existing results: python3 -m scheduler.qos data/experiments/<run>
"""

import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def load_flows(directory, clients, flows_per_client):
    records = []
    for client in range(clients):
        rows = []
        path = Path(directory) / ('flows_%d.txt' % client)
        for line in path.read_text().splitlines():
            fields = line.split()
            if not fields:
                continue
            if len(fields) < 5:
                raise ValueError('invalid flow record in %s' % path)
            size, duration = map(int, fields[:2])
            if size <= 0 or duration < 0:
                raise ValueError('invalid flow size or duration in %s' % path)
            rows.append(dict(client='client%d' % (client + 1),
                             size_bytes=size, fct_us=duration))
        if len(rows) != flows_per_client:
            raise ValueError('QoS requires a completed run: %s has %d/%d flows' %
                             (path, len(rows), flows_per_client))
        records.extend(rows)
    return records


def flow_metrics(records, deadline_ms=None):
    delays = [row['fct_us'] / 1000 for row in records]
    # Recompute as a float: the generator's fifth column truncates Mbps to int.
    rates = [row['size_bytes'] * 8 / row['fct_us'] for row in records if row['fct_us'] > 0]
    met = sum(delay <= deadline_ms for delay in delays) if deadline_ms is not None else None
    return dict(completed_flows=len(records), received_payload_bytes=sum(r['size_bytes'] for r in records),
                mean_fct_ms=statistics.mean(delays) if delays else None,
                p50_fct_ms=percentile(delays, .5), p95_fct_ms=percentile(delays, .95),
                p99_fct_ms=percentile(delays, .99), max_fct_ms=max(delays) if delays else None,
                mean_flow_goodput_mbps=statistics.mean(rates) if rates else None,
                p50_flow_goodput_mbps=percentile(rates, .5),
                p05_flow_goodput_mbps=percentile(rates, .05),
                goodput_samples=len(rates), zero_duration_flows=len(records) - len(rates),
                deadline_ms=deadline_ms, deadline_met_flows=met,
                deadline_miss_pct=(100 * (len(delays) - met) / len(delays)
                                   if delays and met is not None else None))


def groups(records, threshold, deadline_ms=None):
    return {kind: flow_metrics([r for r in records if kind == 'ALL' or
                               ('MICE' if r['size_bytes'] < threshold else 'ELEPHANT') == kind], deadline_ms)
            for kind in ('ALL', 'MICE', 'ELEPHANT')}


def client_window_seconds(log):
    for line in reversed(Path(log).read_text().splitlines()):
        fields = line.split()
        if fields and fields[0] == 'FLOW_TIMING_NS' and len(fields) == 3:
            first, last = map(int, fields[1:])
            if 0 < first < last:
                return (last - first) / 1e9
    return None


def save_run_qos(directory, clients, flows_per_client, threshold, timing, deadline_ms=None):
    if deadline_ms is not None and (not math.isfinite(deadline_ms) or deadline_ms <= 0):
        raise ValueError('QoS deadline must be finite and positive')
    directory = Path(directory)
    records = load_flows(directory, clients, flows_per_client)
    payload = sum(row['size_bytes'] for row in records)
    duration = timing['transfer_time_seconds']
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid transfer duration for QoS')
    result = dict(measurement='client request to completed application-payload reception',
                  percentile_method='nearest_rank', threshold_bytes=threshold,
                  completed_flows=len(records), expected_flows=clients * flows_per_client,
                  completion_rate_pct=100 * len(records) / (clients * flows_per_client),
                  transfer_time_seconds=duration,
                  receiver_goodput_mbps=payload * 8 / duration / 1e6,
                  groups=groups(records, threshold, deadline_ms), clients={},
                  unmeasured=['one_way_packet_delay', 'packet_jitter', 'packet_loss_rate'])
    for index in range(clients):
        name = 'client%d' % (index + 1)
        rows = [row for row in records if row['client'] == name]
        window = client_window_seconds(directory / ('log_%d.txt' % index))
        result['clients'][name] = dict(
            groups=groups(rows, threshold, deadline_ms), active_window_seconds=window,
            receiver_goodput_mbps=(sum(r['size_bytes'] for r in rows) * 8 / window / 1e6
                                   if window is not None else None))
    (directory / 'qos.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def format_value(value):
    return '-' if value is None else '%.3f' % value


def write_qos_outputs(output, results, clients, flows_per_client, threshold, deadline_ms=None):
    output = Path(output)
    csv_rows, pooled, durations, payloads = [], {}, {}, {}
    for run in results:
        directory = output / ('%02d_%s' % (run['repeat'], run['policy']))
        qos = json.loads((directory / 'qos.json').read_text())
        scopes = [('ALL_CLIENTS', qos['groups'], qos['receiver_goodput_mbps'])]
        scopes.extend((name, info['groups'], info['receiver_goodput_mbps'])
                      for name, info in qos['clients'].items())
        for scope, values, receiver_rate in scopes:
            for kind, metrics in values.items():
                csv_rows.append(dict(policy=run['policy'], repeat=run['repeat'], seed=run['seed'],
                                     scope=scope, flow_type=kind, **metrics,
                                     receiver_goodput_mbps=receiver_rate if kind == 'ALL' else None))
        policy = run['policy']
        pooled.setdefault(policy, []).extend(load_flows(directory, clients, flows_per_client))
        durations[policy] = durations.get(policy, 0) + qos['transfer_time_seconds']
        payloads[policy] = payloads.get(policy, 0) + qos['groups']['ALL']['received_payload_bytes']
    if csv_rows:
        with (output / 'qos.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
            writer.writeheader()
            writer.writerows(csv_rows)
    lines = ['수신 측 QoS (완료된 실행의 flow를 합산)',
             '지연은 요청부터 데이터 수신 완료까지의 FCT이며 단위는 ms이다.',
             '정책 종류 완료flow 평균FCT p95FCT p99FCT 최대FCT']
    for policy, records in pooled.items():
        values = groups(records, threshold, deadline_ms)
        for kind, metrics in values.items():
            lines.append('%s %s %d %s %s %s %s' %
                         (policy, kind, metrics['completed_flows'], format_value(metrics['mean_fct_ms']),
                          format_value(metrics['p95_fct_ms']), format_value(metrics['p99_fct_ms']),
                          format_value(metrics['max_fct_ms'])))
            if deadline_ms is not None and metrics['completed_flows']:
                lines.append('  %.3fms 초과 비율: %.3f%%' % (deadline_ms, metrics['deadline_miss_pct']))
        lines.append('  수신 goodput: %.3fMbps (페이로드 합 / 실행 시간 합)' %
                     (payloads[policy] * 8 / durations[policy] / 1e6))
    if not pooled:
        lines.append('완료된 실행 없음')
    lines.extend(['클라이언트별 결과와 flow별 goodput 통계는 qos.csv 및 각 실행의 qos.json에 기록한다.',
                  '단방향 패킷 지연·지터·패킷 손실률은 측정하지 않는다.',
                  '중단·실패한 실행은 위 완료 flow 통계에 포함하지 않으며 status.json에 표시한다.'])
    (output / 'qos_summary.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--qos-deadline-ms', type=float)
    args = parser.parse_args()
    settings = json.loads((args.directory / 'settings.json').read_text())
    deadline = args.qos_deadline_ms if args.qos_deadline_ms is not None else settings.get('qos_deadline_ms')
    clients, per_client = settings['client'], settings['flowsPerClient']
    threshold = settings['flow_threshold']
    results = []
    for timing_file in sorted(args.directory.glob('*/timing.json')):
        timing = json.loads(timing_file.read_text())
        save_run_qos(timing_file.parent, clients, per_client, threshold, timing, deadline)
        results.append(timing)
    write_qos_outputs(args.directory, results, clients, per_client, threshold, deadline)
    print('QoS results: %s' % (args.directory / 'qos_summary.txt'))


if __name__ == '__main__':
    main()
