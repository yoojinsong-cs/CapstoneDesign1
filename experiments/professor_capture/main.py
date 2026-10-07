#!/usr/bin/python3
"""원본 README 조건으로 생성·TShark 캡처·요청 단위 데이터셋 작성.

실행: sudo /usr/bin/python3 experiments/professor_capture/main.py
원본 조건: 2 2 1 1 2 5001 100. 외부 원본 저장소는 수정하지 않는다.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from mininet.log import setLogLevel
from topoBuilder_lb import topoBuilder
from ruleInstaller_lb import ruleInstaller
from flowGenerator_parll import flowGenerator, stop
from prepare_generator import GENERATOR, GENERATOR_COMMIT, PROFESSOR_COMMIT, verify_generator

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results'
SWITCHES = ('Edges1', 'Edgec1', 'Hedges1', 'Hedgec1', 'core1', 'core2')


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=15, **kwargs).stdout


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def snapshot(net, out, phase):
    with (out / ('network-' + phase + '.txt')).open('w') as log:
        for host in net.hosts:
            log.write('\nHOST ' + host.name + '\n')
            for command in (['ip', '-4', 'addr'], ['ip', 'route'],
                            ['sysctl', 'net.ipv4.tcp_congestion_control']):
                stdout, stderr, code = host.pexec(command)
                log.write(stdout + stderr)
                if code:
                    raise RuntimeError('Cannot record host settings')
        for switch in net.switches:
            for subcommand in ('dump-flows', 'dump-groups', 'dump-ports'):
                log.write(run(['ovs-ofctl', '-O', 'OpenFlow13', subcommand, switch.name]))


def configure_offloads(net, out):
    # 오직 이번 Mininet에서 생성한 가상 인터페이스에만 적용한다.
    with (out / 'offloads.txt').open('w') as log:
        for node in net.hosts + net.switches:
            for intf in node.intfList():
                if intf.name == 'lo':
                    continue
                log.write('\n' + node.name + ':' + intf.name + '\nBEFORE\n')
                before, error, code = node.pexec(['ethtool', '-k', intf.name])
                log.write(before + error)
                if code:
                    raise RuntimeError('Cannot inspect offloads: ' + intf.name)
                stdout, stderr, code = node.pexec(
                    ['ethtool', '-K', intf.name, 'tso', 'off', 'gso', 'off', 'gro', 'off'])
                log.write(stdout + stderr)
                if code:
                    raise RuntimeError('Cannot disable offloads: ' + intf.name)
                after, error, code = node.pexec(['ethtool', '-k', intf.name])
                log.write('AFTER\n' + after + error)
                for feature in ('tcp-segmentation-offload', 'generic-segmentation-offload',
                                'generic-receive-offload'):
                    if feature + ': off' not in after:
                        raise RuntimeError('Offload remains enabled: ' + feature)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=int, default=1200)
    parser.add_argument('--check', action='store_true', help='의존성 확인 후 종료. 네트워크는 만들지 않음')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    for name in ('ovs-testcontroller', 'ovs-vsctl', 'ovs-ofctl', 'tshark', 'ethtool',
                 'git', 'ip', 'ss', 'ping', 'stdbuf'):
        if not shutil.which(name):
            parser.error('Missing executable: ' + name)
    try:
        verify_generator()
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    if args.check:
        print('READY: dependencies and pinned generator verified; no network started.')
        return
    if os.geteuid() != 0:
        parser.error('Mininet requires sudo; no network or capture was started.')
    conflicts = set(run(['ovs-vsctl', '--timeout=5', 'list-br']).split()) & set(SWITCHES)
    if conflicts:
        parser.error('Existing Mininet bridges: ' + ', '.join(sorted(conflicts)))

    RESULTS.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='professor-capture-', dir=RESULTS))
    manifest = dict(status='starting', run_id=out.name,
                    created_utc=datetime.now(timezone.utc).isoformat(),
                    original_arguments=[2, 2, 1, 1, 2, 5001, 100],
                    generator_commit=GENERATOR_COMMIT,
                    professor_commit=PROFESSOR_COMMIT,
                    generator_client_sha256=sha(GENERATOR / 'bin/client'),
                    generator_server_sha256=sha(GENERATOR / 'bin/server'),
                    cdf='DCTCP_CDF.txt', cdf_sha256=sha(GENERATOR / 'conf/DCTCP_CDF.txt'),
                    seed_per_client=1, target_rx_mbps_per_client=900,
                    metadata_byteorder=sys.byteorder, timeout_s=args.timeout,
                    tshark_version=run(['tshark', '--version']).splitlines()[0],
                    controller_version=run(['ovs-testcontroller', '--version']).strip(),
                    capture_interface='Hedges1-eth3', offloads='TSO/GSO/GRO disabled on experiment veths',
                    pairs=[],
                    deviations=['OpenFlow13 explicitly selected; unique switch DPIDs',
                                'ARP restricted to core1 tree; IPv4 select groups retain both cores',
                                'Each client has an isolated flow log; servers kept as owned child processes',
                                'Capture and dataset extraction added; only owned processes/net are stopped',
                                'TSO/GSO/GRO disabled for packet observation; original link rates and workload retained'])
    (out / 'source').mkdir()
    for name in ('main.py', 'topoBuilder_lb.py', 'ruleInstaller_lb.py', 'flowGenerator_parll.py',
                 'build_dataset.py', 'prepare_generator.py'):
        shutil.copy2(HERE / name, out / 'source' / name)
    def save_manifest():
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    save_manifest()
    print('RESULT_DIR=' + str(out), flush=True)
    setLogLevel('info')
    net, capture = None, None
    try:
        net = topoBuilder(2, 2, 1, 1, 1, 1, 2)
        net.start()
        if not net.waitConnected(timeout=15):
            raise RuntimeError('Switch/controller connection timeout')
        ruleInstaller(net, 2, 2, 1, 1, 2)
        configure_offloads(net, out)
        snapshot(net, out, 'before')
        with (out / 'ping.log').open('w') as log:
            for index in (1, 2):
                client, server = net.get('client%d' % index, 'server%d' % index)
                stdout, stderr, code = client.pexec(['ping', '-c', '1', '-W', '3', server.IP()])
                log.write(stdout + stderr)
                if code:
                    raise RuntimeError('Pair connectivity failed')
        with ExitStack() as files:
            pcap = files.enter_context((out / 'traffic.pcap').open('wb'))
            log = files.enter_context((out / 'capture.log').open('w'))
            command = ['tshark', '-n', '-q', '-i', 'Hedges1-eth3', '-f', 'tcp port 5001',
                       '-s', '0', '-F', 'pcap', '-w', '-']
            manifest['capture_command'] = command
            capture = subprocess.Popen(command, stdout=pcap, stderr=log,
                                       env={**os.environ, 'LC_ALL': 'C'})
            try:
                for _ in range(100):
                    if capture.poll() is not None:
                        raise RuntimeError('TShark exited before capture readiness')
                    if (out / 'traffic.pcap').stat().st_size >= 24:
                        break
                    time.sleep(.1)
                else:
                    raise TimeoutError('Capture header not written')
                manifest['status'] = 'generating'
                save_manifest()
                manifest['pairs'] = flowGenerator(net, 5001, 100, out, GENERATOR, args.timeout)
                time.sleep(.5)
            finally:
                stop(capture, signal.SIGINT)
            if capture.returncode != 0:
                raise RuntimeError('TShark exit code: %s' % capture.returncode)
        snapshot(net, out, 'after')
        manifest['pcap_sha256'] = sha(out / 'traffic.pcap')
        manifest['status'] = 'captured'
        save_manifest()
    except BaseException as exc:
        manifest['status'] = 'failed'
        manifest['error'] = str(exc)
        raise
    finally:
        try:
            stop(capture, signal.SIGINT)
            if net is not None:
                net.stop()
        finally:
            save_manifest()
            uid, gid = int(os.environ.get('SUDO_UID', '0')), int(os.environ.get('SUDO_GID', '0'))
            for path in [out, *out.rglob('*')]:
                os.chown(path, uid, gid)

    # 분석은 일반 사용자 권한으로 수행한다. 네트워크는 이미 종료한 상태다.
    uid, gid = int(os.environ.get('SUDO_UID', '0')), int(os.environ.get('SUDO_GID', '0'))
    command = ['/usr/bin/python3', str(HERE / 'build_dataset.py'), str(out)]
    options = dict(user=uid, group=gid, extra_groups=[])
    result = subprocess.run(command, **options)
    manifest['status'] = 'complete' if result.returncode == 0 else 'dataset_failed'
    save_manifest()
    os.chown(out / 'manifest.json', uid, gid)
    if result.returncode:
        raise SystemExit(result.returncode)
    print('COMPLETE=' + str(out), flush=True)


if __name__ == '__main__':
    main()
