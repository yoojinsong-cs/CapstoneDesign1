"""Compare transfer completion time: sudo python3 experiment.py 2 2 1 1 2 5001 50."""

import argparse
from collections import Counter
import csv
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

from flowGenerator_parll import flowGenerator, formatProgress
from ruleInstaller_lb import ruleInstaller
from scheduler.benchmark import restore_owner
from scheduler.diagnostics import NetworkDiagnostics, write_diagnostic_report
from scheduler.installer import (POLICY_VERSION, PRIORITY_POLICY, SchedulerConfig,
                                 SwitchScheduler, ensure_module)
from scheduler.mock_classifier import DEFAULT_CLASSIFIER_DELAY
from scheduler.qos import save_run_qos, write_qos_outputs
from topoBuilder_lb import topoBuilder


SCRIPTS = Path(__file__).resolve().parent
EXPERIMENT_LINK_MBPS = 100


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('server', 'client', 'serverEdge', 'clientEdge', 'paths', 'port', 'flowsPerClient'):
        parser.add_argument(name, type=int)
    parser.add_argument('--policy', choices=('both', 'priority', 'legacy', 'fifo', 'fair'), default='both')
    comparison = parser.add_mutually_exclusive_group()
    comparison.add_argument('--diagnose', action='store_true',
                            help='compare legacy/fifo/fair/priority with fixed paths and network counters')
    comparison.add_argument('--compare-fair', action='store_true',
                            help='compare fair/priority with fixed paths and network counters')
    parser.add_argument('--repeat', '--repeats', dest='repeat', type=int, default=1)
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--classifier-delay', type=float, default=DEFAULT_CLASSIFIER_DELAY)
    parser.add_argument('--flow-threshold', type=int, default=1_000_000)
    parser.add_argument('--qos-deadline-ms', type=float,
                        help='optional request-to-completion deadline for QoS miss rates')
    parser.add_argument('--load-mbps', type=float, default=900)
    parser.add_argument('--cdf', default=str(SCRIPTS / 'TrafficGenerator/conf/DCTCP_CDF.txt'))
    parser.add_argument('--output-dir')
    args = parser.parse_args(argv)
    if (args.diagnose or args.compare_fair) and args.policy != 'both':
        parser.error('comparison mode selects policies automatically; omit --policy')
    if min(args.server, args.client, args.serverEdge, args.clientEdge, args.paths,
           args.flowsPerClient, args.repeat, args.seed) < 1:
        parser.error('counts and seed must be positive')
    if max(args.server, args.client) > 254 or not 1 <= args.port <= 65535:
        parser.error('at most 254 hosts per side; port must be 1..65535')
    if not math.isfinite(args.load_mbps) or args.load_mbps <= 0:
        parser.error('--load-mbps must be positive and finite')
    if args.qos_deadline_ms is not None and (
            not math.isfinite(args.qos_deadline_ms) or args.qos_deadline_ms <= 0):
        parser.error('--qos-deadline-ms must be positive and finite')
    try:
        SchedulerConfig(delay_seconds=args.classifier_delay, threshold_bytes=args.flow_threshold)
    except ValueError as exc:
        parser.error(str(exc))
    args.cdf = str(Path(args.cdf).resolve())
    if not Path(args.cdf).is_file():
        parser.error('CDF not found: %s' % args.cdf)
    return args


def policies_for_repeat(args, repeat):
    if args.diagnose:
        policies = ('legacy', 'fifo', 'fair', 'priority')
    elif args.compare_fair:
        policies = ('fair', 'priority')
    elif args.policy == 'both':
        policies = ('priority', 'legacy')
    else:
        return (args.policy,)
    return policies if repeat % 2 else tuple(reversed(policies))


def workload(directory, clients, flows_per_client):
    sizes = Counter()
    for index in range(clients):
        lines = (directory / ('flows_%d.txt' % index)).read_text().splitlines()
        rows = [line.split() for line in lines if line.strip()]
        if len(rows) != flows_per_client:
            raise RuntimeError('client%d: expected %d completed flows, got %d' %
                               (index + 1, flows_per_client, len(rows)))
        for fields in rows:
            if len(fields) < 5 or int(fields[0]) < 1 or int(fields[1]) < 0:
                raise RuntimeError('invalid completed-flow record')
            sizes[index, int(fields[0])] += 1
    return sizes


class ExperimentProgress:
    def __init__(self, output, policy, repeat, repeats, run_number, total_runs):
        self.output = output
        self.state = dict(policy=policy, repeat=repeat, repeats=repeats,
                          run_number=run_number, total_runs=total_runs)
        self.prefix = '[실험 %d/%d | %s %d/%d회]' % (run_number, total_runs, policy, repeat, repeats)
        self.inline = False
        self.width = 0
        self.monitor = None

    def save(self):
        pending = self.output / 'progress.json.tmp'
        pending.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
        pending.replace(self.output / 'progress.json')

    def stage(self, phase):
        if self.inline:
            print(flush=True)
            self.inline = False
        self.state['phase'] = phase
        self.save()
        print('%s %s' % (self.prefix, phase), flush=True)

    def update(self, progress, per_client, elapsed, idle):
        if self.monitor:
            self.monitor.sample()
        self.state.update(phase='전송 중', completed_flows=sum(progress),
                          total_flows=len(progress) * per_client, clients=progress,
                          elapsed_seconds=round(elapsed, 3),
                          since_last_completion_seconds=round(idle, 3))
        self.save()
        line = self.prefix + ' ' + formatProgress(progress, per_client, elapsed, idle)
        if sys.stdout.isatty():
            print('\r' + line.ljust(self.width), end='', flush=True)
            self.width = max(self.width, len(line))
            self.inline = True
        else:
            print(line, flush=True)


def run_one(args, policy, repeat, output, run_number=1, total_runs=1):
    seed = args.seed + repeat - 1
    directory = output / ('%02d_%s' % (repeat, policy))
    config = SchedulerConfig(mode=policy if policy != 'legacy' else 'priority',
                             rate_mbps=EXPERIMENT_LINK_MBPS,
                             delay_seconds=args.classifier_delay,
                             threshold_bytes=args.flow_threshold)
    network, scheduler = None, None
    diagnostics, timing = None, None
    completed = False
    progress = ExperimentProgress(output, policy, repeat, args.repeat, run_number, total_runs)
    print('\nSTART policy=%s repeat=%d seed=%d flows=%d link_mbps=%d\n' %
          (policy, repeat, seed, args.client * args.flowsPerClient, EXPERIMENT_LINK_MBPS), flush=True)
    try:
        progress.stage('토폴로지 구성 중')
        network = topoBuilder(args.server, args.client, args.serverEdge,
                              args.clientEdge, 1, 1, args.paths,
                              edge_core_mbps=EXPERIMENT_LINK_MBPS)
        network.start()
        progress.stage('스위치 경로 설정 중')
        ruleInstaller(network, args.server, args.client, args.serverEdge,
                      args.clientEdge, args.paths, fixed_paths=args.diagnose or args.compare_fair)
        if policy != 'legacy':
            progress.stage('%s 스케줄러 적용 중' % policy)
            scheduler = SwitchScheduler(config, directory,
                                        experiment=dict(vars(args), seed=seed, repeat=repeat,
                                                        link_mbps=EXPERIMENT_LINK_MBPS))
            scheduler.start(network)
        else:
            directory.mkdir()
            (directory / 'manifest.json').write_text(json.dumps(
                dict(policy=policy, seed=seed, link_mbps=EXPERIMENT_LINK_MBPS,
                     arguments=vars(args)), indent=2) + '\n')
        if args.diagnose or args.compare_fair:
            progress.stage('큐·TCP·경로 통계 기록 시작')
            diagnostics = NetworkDiagnostics(network, directory)
            diagnostics.start()
            progress.monitor = diagnostics
        progress.stage('서버 및 트래픽 준비 중')
        timing = flowGenerator(network, args.port, args.flowsPerClient,
                               cdfPath=args.cdf, baseSeed=seed, loadMbps=args.load_mbps,
                               healthCheck=scheduler.check if scheduler else None,
                               capture=False, progressCallback=progress.update)
        progress.stage('전송 완료 · 결과 확인 및 저장 중')
        if diagnostics:
            diagnostics.finish(timing)
        if scheduler:
            scheduler.check()
        for index in range(args.client):
            for filename in ('flows_%d.txt' % index, 'log_%d.txt' % index):
                shutil.copy2(SCRIPTS / filename, directory / filename)
        signature = workload(directory, args.client, args.flowsPerClient)
        expected = args.client * args.flowsPerClient
        if timing['completed_flows'] != expected:
            raise RuntimeError('not all requested flows completed')
        result = dict(policy=policy, repeat=repeat, seed=seed, link_mbps=EXPERIMENT_LINK_MBPS,
                      scheduler_policy_version=POLICY_VERSION if scheduler else None,
                      total_flows=expected, completed_flows=timing['completed_flows'],
                      transfer_time_seconds=timing['transfer_time_seconds'],
                      first_request_ns=timing['first_request_ns'],
                      last_completion_ns=timing['last_completion_ns'],
                      total_payload_bytes=sum(size * count for (_, size), count in signature.items()))
        qos = save_run_qos(directory, args.client, args.flowsPerClient,
                           args.flow_threshold, timing, args.qos_deadline_ms)
        result.update(receiver_goodput_mbps=qos['receiver_goodput_mbps'],
                      mean_fct_ms=qos['groups']['ALL']['mean_fct_ms'],
                      mice_p95_fct_ms=qos['groups']['MICE']['p95_fct_ms'],
                      mice_p99_fct_ms=qos['groups']['MICE']['p99_fct_ms'])
        (directory / 'timing.json').write_text(json.dumps(result, indent=2) + '\n')
        print('RESULT policy=%s repeat=%d completed=%d/%d total_transfer_seconds=%.6f' %
              (policy, repeat, expected, expected, result['transfer_time_seconds']), flush=True)
        completed = True
        return result, signature
    finally:
        try:
            if diagnostics:
                diagnostics.finish(timing)
        finally:
            try:
                progress.stage('네트워크 정리 중')
            finally:
                try:
                    if scheduler:
                        scheduler.stop()
                finally:
                    if network:
                        network.stop()
        progress.stage('실행 완료' if completed else '실행 중단/실패')


def write_report(output, args, results, state):
    status = {'running': '진행 중', 'complete': '완료',
              'interrupted': '중단', 'failed': '실패'}[state['status']]
    lines = [
        '실험 결과',
        '상태: %s (%d/%d회 완료)' %
        (status, state['completed_runs'], state['requested_runs']),
        '토폴로지: 서버 %d, 클라이언트 %d, 서버 엣지 %d, 클라이언트 엣지 %d, 경로 %d' %
        (args.server, args.client, args.serverEdge, args.clientEdge, args.paths),
        '회당 flow: %d개 (클라이언트당 %d개)' %
        (args.client * args.flowsPerClient, args.flowsPerClient),
        '링크: Host 1Gbps, Edge-Core %dMbps' % EXPERIMENT_LINK_MBPS,
        'CDF: %s / 모의 분류 지연: %s초' % (Path(args.cdf).name, args.classifier_delay),
        'priority 정책: UNKNOWN/MICE 우선, 분류된 ELEPHANT는 낮은 등급 (v%s)' % POLICY_VERSION,
        '경로: %s' % ('IP 쌍별 고정' if args.diagnose or args.compare_fair else 'ECMP'),
        '',
        '전체 전송 시간 (첫 요청 ~ 마지막 flow 수신 완료)',
    ]
    for result in results:
        lines.append('%d회 %s: %.6f초, 완료 %d/%d개, seed=%d' %
                     (result['repeat'], result['policy'], result['transfer_time_seconds'],
                      result['completed_flows'], result['total_flows'], result['seed']))
    if not results:
        lines.append('완료된 실행 없음')
    else:
        lines.extend(['', '평균 (완료된 실행 기준)'])
        for policy in ('priority', 'legacy', 'fifo', 'fair'):
            times = [r['transfer_time_seconds'] for r in results if r['policy'] == policy]
            if times:
                lines.append('%s: %.6f초 (%d회)' % (policy, statistics.mean(times), len(times)))
    if state.get('reason'):
        lines.extend(['', '사유: %s' % state['reason']])
    if (output / 'qos_summary.txt').exists():
        lines.extend(['', (output / 'qos_summary.txt').read_text(encoding='utf-8').rstrip()])
    (output / 'summary.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run(args):
    if os.geteuid() != 0:
        raise RuntimeError('run this script with sudo')
    output = (Path(args.output_dir).resolve() if args.output_dir else
              SCRIPTS / 'data/experiments' / (time.strftime('%Y%m%d_%H%M%S') + '_%d' % os.getpid()))
    if output.exists():
        raise RuntimeError('output directory already exists: %s' % output)
    os.chdir(SCRIPTS)
    bridges = set(subprocess.check_output(['ovs-vsctl', 'list-br'], text=True).splitlines())
    names = {'Hedges1', 'Hedgec1'}
    for prefix, count in (('Edges', args.serverEdge), ('Edgec', args.clientEdge), ('core', args.paths)):
        names.update(prefix + str(i) for i in range(1, count + 1))
    if bridges & names:
        raise RuntimeError('existing experiment switches: %s' % ', '.join(sorted(bridges & names)))
    subprocess.run(['make'], cwd=SCRIPTS / 'TrafficGenerator', check=True)
    if args.policy != 'legacy':
        ensure_module(args.port)
    output.mkdir(parents=True)
    state = dict(status='running', completed_runs=0,
                 requested_runs=args.repeat * len(policies_for_repeat(args, 1)))
    results, signatures = [], {}
    try:
        (output / 'settings.json').write_text(json.dumps(
            dict(vars(args), link_mbps=EXPERIMENT_LINK_MBPS,
                 scheduler_policy_version=POLICY_VERSION, priority_policy=PRIORITY_POLICY,
                 fixed_paths=args.diagnose or args.compare_fair), indent=2) + '\n')
        for repeat in range(1, args.repeat + 1):
            for policy in policies_for_repeat(args, repeat):
                state['current_run'] = '%02d_%s' % (repeat, policy)
                (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
                result, signature = run_one(args, policy, repeat, output,
                                            run_number=len(results) + 1,
                                            total_runs=state['requested_runs'])
                if repeat in signatures and signature != signatures[repeat]:
                    raise RuntimeError('paired policies generated different flow sizes')
                signatures[repeat] = signature
                results.append(result)
                with (output / 'summary.csv').open('w', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(result))
                    writer.writeheader()
                    writer.writerows(results)
                state['completed_runs'] = len(results)
                write_qos_outputs(output, results, args.client, args.flowsPerClient,
                                  args.flow_threshold, args.qos_deadline_ms)
                write_report(output, args, results, state)
                if args.diagnose or args.compare_fair:
                    write_diagnostic_report(output, results)
                print('[전체 진행] %d/%d회 완료 (%.1f%%)' %
                      (len(results), state['requested_runs'],
                       100 * len(results) / state['requested_runs']), flush=True)
        state['status'] = 'complete'
        print('\npolicy     runs  mean_total_transfer_seconds')
        for policy in ('priority', 'legacy', 'fifo', 'fair'):
            times = [r['transfer_time_seconds'] for r in results if r['policy'] == policy]
            if times:
                print('%-10s %4d  %.6f' % (policy, len(times), statistics.mean(times)))
        print('Results: %s' % (output / 'summary.csv'), flush=True)
        print('Summary: %s' % (output / 'summary.txt'), flush=True)
        return results
    except BaseException as exc:
        state.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                     reason=str(exc))
        raise
    finally:
        (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
        write_qos_outputs(output, results, args.client, args.flowsPerClient,
                          args.flow_threshold, args.qos_deadline_ms)
        write_report(output, args, results, state)
        if args.diagnose or args.compare_fair:
            write_diagnostic_report(output, results)
        restore_owner(output)


def main():
    args = parse_args()

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        run(args)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit('ERROR: %s' % exc)


if __name__ == '__main__':
    main()
