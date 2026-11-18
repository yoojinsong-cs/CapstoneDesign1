"""Summarize a data/scheduler/<run> directory."""

import argparse
import json
import math
from pathlib import Path
import statistics


def summarize(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    threshold = manifest['config']['threshold_bytes']
    groups = {'MICE': [], 'ELEPHANT': []}
    for path in sorted(directory.glob('flows_*.txt')):
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 5:
                size, fct_us = map(int, fields[:2])
                groups['ELEPHANT' if size >= threshold else 'MICE'].append(fct_us)
    events_path = directory / 'classifications.jsonl'
    events = ([json.loads(line) for line in events_path.read_text().splitlines()]
              if events_path.exists() else [])
    decisions = [event for event in events if event['event'] == 'decision']
    print('mode=%s; classifier delay=%ss; decisions=%d' % (
        manifest['config']['mode'], manifest['config']['delay_seconds'], len(decisions)))
    for kind, values in groups.items():
        if not values:
            print('%s: no TrafficGenerator FCT records' % kind)
            continue
        values.sort()
        p99 = values[math.ceil(len(values) * 0.99) - 1]
        print('%s: n=%d mean=%.3fms p50=%.3fms p99=%.3fms' % (
            kind, len(values), statistics.mean(values) / 1000,
            statistics.median(values) / 1000, p99 / 1000))
    stats_path = directory / 'final_stats.txt'
    if not stats_path.exists():
        return
    interfaces = {port['ifindex'] for port in manifest.get('interfaces', [])}
    if not interfaces:
        interfaces = {manifest['ifindex']}
    for line in stats_path.read_text().splitlines():
        fields = line.split()
        if fields and fields[0] == 'PORT' and int(fields[1]) in interfaces:
            print('switch ifindex=%s: sent=%s bytes fallback/control=%s '
                  'cross-boundary=%s queued=%s drops=%s max service wait=%.3fms' % (
                      fields[1], fields[3], *fields[5:9], int(fields[9]) / 1_000_000))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    summarize(parser.parse_args().directory)
