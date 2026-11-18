"""Bridge kernel flow observations to independent delayed mock decisions."""

import argparse
import asyncio
import errno
import json
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .mock_classifier import DEFAULT_CLASSIFIER_DELAY, FlowKey, FlowType, MockClassifier


@dataclass(frozen=True)
class Observation:
    ifindex: int
    cookie: int
    client_ip: str
    flow_id: int
    flow_size: int
    first_ns: int
    kind: int
    queued_packets: int
    queued_bytes: int
    sent_bytes: int
    boosts: int  # Legacy statistics column; strict-priority kernels report zero.
    max_wait_ns: int
    retired: int


def observations(snapshot, interfaces):
    for line in snapshot.splitlines():
        fields = line.split()
        if not fields or fields[0] != 'FLOW':
            continue
        if len(fields) != 14:
            raise ValueError('invalid flowprio record: %r' % line)
        ifindex, cookie = map(int, fields[1:3])
        if ifindex not in interfaces:
            continue
        yield Observation(ifindex, cookie, fields[3],
                          *map(int, fields[4:]))


def class_command(observation, flow_type):
    kind = {FlowType.MICE: 1, FlowType.ELEPHANT: 2}[flow_type]
    return 'class %d %d %d\n' % (
        observation.ifindex, observation.cookie, kind)


async def serve(args):
    directory = Path(args.logdir)
    directory.mkdir(parents=True, exist_ok=True)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    classifier = MockClassifier(args.delay, args.threshold)
    jobs = {}
    seen = set()
    failure = []
    with (directory / 'classifications.jsonl').open('w', buffering=1) as log:
        def record(event, **values):
            log.write(json.dumps(dict(event=event, time=time.monotonic(),
                                      **values), sort_keys=True) + '\n')

        def callback(observation, decision):
            try:
                with Path(args.proc).open('w') as control:
                    control.write(class_command(observation, decision.flow_type))
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    record('stale', cookie=observation.cookie)
                    return
                raise
            record('decision', ifindex=observation.ifindex,
                   cookie=observation.cookie, flow_id=decision.key.flow_id,
                   client_ip=decision.key.client_ip,
                   flow_type=decision.flow_type.value,
                   observed_at=decision.observed_at,
                   decided_at=decision.decided_at,
                   delay=decision.decided_at - decision.observed_at)

        def finished(identity, task):
            jobs.pop(identity, None)
            if not task.cancelled() and task.exception() is not None:
                failure.append(task.exception())
                stopped.set()

        try:
            # Probe access before reporting readiness to main.py.
            Path(args.proc).read_text()
            record('started', delay=args.delay, threshold=args.threshold)
            Path(args.ready).write_text('ready\n')
            while not stopped.is_set():
                snapshot = Path(args.proc).read_text()
                for observation in observations(snapshot, set(args.interfaces)):
                    identity = observation.ifindex, observation.cookie
                    key = FlowKey(args.run_id, observation.client_ip,
                                  observation.flow_id)
                    if observation.retired:
                        classifier.discard(key)
                        continue
                    if identity in seen or observation.kind:
                        continue
                    seen.add(identity)
                    record('observed', **asdict(observation))
                    task = classifier.submit(
                        key, observation.flow_size,
                        lambda decision, obs=observation: callback(obs, decision),
                        observed_at=observation.first_ns / 1_000_000_000)
                    jobs[identity] = task
                    task.add_done_callback(
                        lambda task, identity=identity: finished(identity, task))
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=args.poll)
                except asyncio.TimeoutError:
                    pass
            if failure:
                raise failure[0]
        finally:
            await classifier.aclose()
            stats = Path(args.stats).read_text()
            (directory / 'final_stats.txt').write_text(stats)
            record('stopped', observed_flows=len(seen))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interfaces', type=int, nargs='+', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--logdir', required=True)
    parser.add_argument('--ready', required=True)
    parser.add_argument('--delay', type=float, default=DEFAULT_CLASSIFIER_DELAY)
    parser.add_argument('--threshold', type=int, default=1_000_000)
    parser.add_argument('--poll', type=float, default=0.02)
    parser.add_argument('--proc', default='/proc/net/flowprio')
    parser.add_argument('--stats', default='/proc/net/flowprio_stats')
    args = parser.parse_args()
    if args.poll <= 0:
        parser.error('--poll must be positive')
    asyncio.run(serve(args))


if __name__ == '__main__':
    main()
