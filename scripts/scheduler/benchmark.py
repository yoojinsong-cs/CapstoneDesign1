"""Run paired priority/legacy experiments on the original two-path topology.

From scripts/: sudo python3 -m scheduler.benchmark
Use --prepare-only to save the ten commands without starting Mininet.
"""

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

from .installer import ensure_module
from .mock_classifier import DEFAULT_CLASSIFIER_DELAY


SCRIPTS = Path(__file__).resolve().parent.parent


def plan_runs(output, total_flows=100, repeats=5, base_seed=1729):
    if total_flows < 2 or total_flows % 2:
        raise ValueError('total flows must be positive and divisible by 2 clients')
    if repeats < 1 or base_seed < 1:
        raise ValueError('repeats and seed must be positive')
    runs = []
    for pair in range(repeats):
        # Alternate the order to avoid always measuring a policy first.
        modes = ('priority', 'legacy') if pair % 2 == 0 else ('legacy', 'priority')
        for mode in modes:
            name = '%02d_%s' % (pair + 1, mode)
            command = [sys.executable, '-u', str(SCRIPTS / 'main.py'),
                       '2', '2', '1', '1', '2', '5001', str(total_flows // 2),
                       '--scheduler', mode, '--classifier-delay', str(DEFAULT_CLASSIFIER_DELAY),
                       '--seed', str(base_seed + pair), '--load-mbps', '900',
                       '--cdf', 'TrafficGenerator/conf/VL2_CDF.txt',
                       '--run-dir', str(output / name)]
            runs.append(dict(name=name, pair=pair + 1, mode=mode,
                             seed=base_seed + pair, command=command))
    return runs


def read_flows(directory, total_flows):
    flows = []
    for client in range(2):
        rows = []
        for line in (directory / ('flows_%d.txt' % client)).read_text().splitlines():
            fields = line.split()
            if not fields:
                continue
            if len(fields) < 5:
                raise ValueError('invalid FCT record: %r' % line)
            size, fct_us = map(int, fields[:2])
            if size < 1 or fct_us < 0:
                raise ValueError('invalid size or completion time')
            rows.append(dict(client=client, size=size, fct_us=fct_us))
        if len(rows) != total_flows // 2:
            raise ValueError('client %d completed %d flows, expected %d' %
                             (client + 1, len(rows), total_flows // 2))
        flows.extend(rows)
    return flows


def metrics(flows):
    result = {}
    for kind in ('ALL', 'MICE', 'ELEPHANT'):
        times = sorted(flow['fct_us'] / 1000 for flow in flows
                       if kind == 'ALL' or
                       ('MICE' if flow['size'] < 1_000_000 else 'ELEPHANT') == kind)
        result[kind] = dict(n=len(times), mean_ms=statistics.mean(times) if times else None,
                            p50_ms=statistics.median(times) if times else None,
                            p99_ms=times[math.ceil(len(times) * .99) - 1] if times else None)
    return result


def write_summary(output, results):
    with (output / 'summary.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['pair', 'mode', 'seed', 'kind', 'n', 'mean_ms', 'p50_ms', 'p99_ms'])
        for result in results:
            for kind, values in result['metrics'].items():
                writer.writerow([result['pair'], result['mode'], result['seed'], kind,
                                 values['n'], values['mean_ms'], values['p50_ms'], values['p99_ms']])
    (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')


def restore_owner(output):
    if not os.environ.get('SUDO_UID'):
        return
    uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
    for path in [output, *output.rglob('*')]:
        os.chown(path, uid, gid)


def stop_requested_run(output):
    """Recover this benchmark's own processes if normal interrupt cleanup hangs."""
    request = output / 'stop.requested'
    if not request.exists():
        return False
    if os.geteuid() != 0:
        raise RuntimeError('root is required to stop the benchmark processes')
    diagnostics = output / 'interrupted_diagnostics'
    diagnostics.mkdir(exist_ok=True)
    for switch in ('Edges1', 'core1', 'core2', 'Edgec1'):
        result = subprocess.run(['ovs-ofctl', 'dump-flows', switch],
                                capture_output=True, text=True, timeout=10)
        (diagnostics / (switch + '_flows.txt')).write_text(result.stdout + result.stderr)
    processes, parents = {}, {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            argv = (path / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            argv = [arg.decode(errors='replace') for arg in argv]
            parents[int(path.name)] = int((path / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError):
            continue
        if argv and Path(argv[0]).name.startswith('python'):
            processes[int(path.name)] = argv
    roots = set()
    for pid, argv in processes.items():
        if pid == os.getpid() or 'scheduler.benchmark' not in argv:
            continue
        if '--output' in argv and Path(argv[argv.index('--output') + 1]).resolve() == output:
            roots.add(pid)
    descendants = set(roots)
    while True:
        found = {pid for pid, parent in parents.items() if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1)
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if roots:
        result = subprocess.run(['mn', '-c'], capture_output=True, text=True, timeout=30)
        (diagnostics / 'cleanup.log').write_text(result.stdout + result.stderr)
    for client in (1, 2):
        capture = Path('/tmp/client%d.pcap' % client)
        if capture.exists():
            shutil.copyfile(capture, diagnostics / capture.name)
    request.unlink()
    state = dict(status='interrupted', reason='Stopped for network-path diagnosis',
                 completed_runs=len(json.loads((output / 'results.json').read_text()))
                 if (output / 'results.json').exists() else 0, requested_runs=10)
    (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
    restore_owner(output)
    print('Stopped benchmark processes; preserved diagnostics in %s' % diagnostics)
    return True


def run(args):
    output = Path(args.output).resolve() if args.output else (
        SCRIPTS / 'data' / 'comparisons' /
        ('%s_%dflows_%d' % (time.strftime('%Y%m%d_%H%M%S'), args.flows, os.getpid())))
    if stop_requested_run(output):
        return
    runs = plan_runs(output, args.flows, args.repeats, args.seed)
    output.mkdir(parents=True, exist_ok=True)
    if any((output / item['name']).exists() for item in runs):
        raise RuntimeError('experiment directories already exist; choose a new --output')
    plan = dict(total_flows=args.flows, flows_per_client=args.flows // 2,
                repeats_per_policy=args.repeats, classifier_delay=DEFAULT_CLASSIFIER_DELAY,
                topology=dict(servers=2, clients=2, server_edges=1, client_edges=1,
                              paths=2, host_link_mbps=1000, edge_core_link_mbps=10),
                cdf='VL2', offered_load_mbps_per_client=900, runs=runs)
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    state = dict(status='prepared', completed_runs=0, requested_runs=len(runs))
    results = []
    try:
        print('Plan: %s (%d flows/run, %d runs)' % (output, args.flows, len(runs)), flush=True)
        if args.prepare_only:
            return
        if os.geteuid() != 0:
            raise RuntimeError('Mininet requires root: run sudo python3 -m scheduler.benchmark')
        for tool in ('ovs-vsctl', 'tc', 'ethtool', 'tshark', 'make'):
            if not shutil.which(tool):
                raise RuntimeError('required command missing: %s' % tool)
        bridges = subprocess.check_output(['ovs-vsctl', 'list-br'], text=True).splitlines()
        occupied = set(bridges) & {'Edges1', 'Edgec1', 'Hedges1', 'Hedgec1', 'core1', 'core2'}
        if occupied:
            raise RuntimeError('existing experiment switches: %s' % ', '.join(sorted(occupied)))
        # Build/load once and fail before any experiment if the kernel is not ready.
        ensure_module(5001)
        state['status'] = 'running'
        (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
        for item in runs:
            state['current_run'] = item['name']
            (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
            print('Starting %s (seed %d)' % (item['name'], item['seed']), flush=True)
            logfile = output / (item['name'] + '.log')
            with logfile.open('w') as stream:
                process = subprocess.Popen(item['command'], cwd=SCRIPTS,
                                           stdout=stream, stderr=subprocess.STDOUT)
                try:
                    while process.poll() is None:
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            print('Running %s; log: %s' % (item['name'], logfile), flush=True)
                except BaseException:
                    if process.poll() is None:
                        # Let main.py clean up the network and capture subprocesses.
                        process.send_signal(2)
                        process.wait()
                    raise
            if process.returncode:
                raise RuntimeError('%s failed (%d); inspect %s' %
                                   (item['name'], process.returncode, logfile))
            flows = read_flows(output / item['name'], args.flows)
            if item['mode'] == 'priority':
                events = output / item['name'] / 'classifications.jsonl'
                for line in events.read_text().splitlines():
                    event = json.loads(line)
                    if event['event'] == 'decision' and event['delay'] < DEFAULT_CLASSIFIER_DELAY:
                        raise RuntimeError('classification delivered before 0.5 seconds')
            result = dict(name=item['name'], pair=item['pair'], mode=item['mode'],
                          seed=item['seed'], flows=flows, metrics=metrics(flows))
            for previous in results:
                if previous['pair'] == item['pair']:
                    if Counter((f['client'], f['size']) for f in previous['flows']) != Counter(
                            (f['client'], f['size']) for f in flows):
                        raise RuntimeError('paired flow sizes differ; comparison is invalid')
            results.append(result)
            state['completed_runs'] = len(results)
            write_summary(output, results)
            (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
        state['status'] = 'complete'
        print('Completed %d experiments. Summary: %s' % (len(results), output / 'summary.csv'))
    except BaseException as exc:
        state.update(status='blocked' if not results else 'failed', reason=str(exc))
        raise
    finally:
        (output / 'status.json').write_text(json.dumps(state, indent=2) + '\n')
        restore_owner(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flows', type=int, default=100, help='total flow count per run')
    parser.add_argument('--repeats', type=int, default=5, help='runs per policy')
    parser.add_argument('--seed', type=int, default=1729)
    parser.add_argument('--output')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    try:
        run(args)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
