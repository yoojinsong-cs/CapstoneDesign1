#!/usr/bin/python3
"""TShark pcap을 TCP 재조립해 요청 정답과 N=1..10 초기 응답 특징으로 변환.

Python 표준 라이브러리와 TShark만 사용한다. root가 필요 없다.
"""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


@dataclass
class Packet:
    frame: int
    time_ns: int
    seq: int
    ip_len: int
    payload: bytes


def reassemble(packets):
    """현재 생성기는 SYN 다음 sequence=1부터 송신. gaps/conflicting overlaps 거부."""
    data = bytearray()
    for packet in sorted(packets, key=lambda p: (p.seq, p.frame)):
        offset = packet.seq - 1
        if offset < 0 or offset > len(data):
            raise ValueError('TCP capture gap at frame %s, sequence %s' % (packet.frame, packet.seq))
        overlap = min(len(data) - offset, len(packet.payload))
        if data[offset:offset + overlap] != packet.payload[:overlap]:
            raise ValueError('Conflicting TCP retransmission at frame %s' % packet.frame)
        data.extend(packet.payload[overlap:])
    return data


def decode_exchange(client_packets, server_packets, byteorder):
    request_data, response_data = reassemble(client_packets), reassemble(server_packets)
    metadata = struct.Struct('<4I' if byteorder == 'little' else '>4I')
    if len(request_data) % metadata.size:
        raise ValueError('Truncated request metadata')
    decoded, offset = [], 0
    for request_offset in range(0, len(request_data), metadata.size):
        values = metadata.unpack_from(request_data, request_offset)
        request_id, size, tos, rate = values
        if offset + metadata.size + size > len(response_data):
            raise ValueError('Incomplete response for request %s' % request_id)
        if metadata.unpack_from(response_data, offset) != values:
            raise ValueError('Request/response metadata mismatch')
        body_start = offset + metadata.size + 1  # TCP relative sequence starts at 1
        body_end = body_start + size
        decoded.append(dict(request_id=request_id, final_bytes=size, tos=tos, rate_mbps=rate,
                            request_seq_start=request_offset + 1,
                            body_seq_start=body_start, body_seq_end=body_end))
        offset += metadata.size + size
    if offset != len(response_data):
        raise ValueError('Unmatched response bytes')
    return decoded


def unseen(start, end, seen):
    """이미 관측한 구간을 빼고 이번 패킷에서 처음 본 바이트 수를 반환한다."""
    if end <= start:
        return 0
    pieces = [(start, end)]
    for left, right in seen:
        remaining = []
        for a, b in pieces:
            if b <= left or a >= right:
                remaining.append((a, b))
            else:
                if a < left:
                    remaining.append((a, left))
                if b > right:
                    remaining.append((right, b))
        pieces = remaining
    count = sum(b - a for a, b in pieces)
    merged = []
    for a, b in sorted(seen + [(start, end)]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    seen[:] = merged
    return count


def body_observations(packets, start, end):
    seen, observations = [], []
    for packet in sorted(packets, key=lambda p: p.frame):
        a, b = max(start, packet.seq), min(end, packet.seq + len(packet.payload))
        count = unseen(a, b, seen)
        if count:
            observations.append((packet, count))
    if sum(count for _, count in observations) != end - start:
        raise ValueError('Body byte accounting mismatch')
    return observations


def feature_row(sample_id, n, observations):
    prefix = observations[:n]
    row = dict(sample_id=sample_id, n=n, observed_count=len(prefix))
    for index in range(10):
        present = index < len(prefix)
        row['mask_%d' % (index + 1)] = int(present)
        row['body_bytes_%d' % (index + 1)] = prefix[index][1] if present else 0
        row['ip_len_%d' % (index + 1)] = prefix[index][0].ip_len if present else 0
        delta = (prefix[index][0].time_ns - prefix[index - 1][0].time_ns
                 if present and index else 0)
        if delta < 0:
            raise ValueError('Nonmonotonic capture timestamps')
        row['iat_ms_%d' % (index + 1)] = delta / 1e6
    row['prefix_bytes'] = sum(count for _, count in prefix)
    row['prefix_elapsed_ms'] = ((prefix[-1][0].time_ns - prefix[0][0].time_ns) / 1e6
                                if len(prefix) > 1 else 0)
    return row


def write_csv(path, rows):
    if not rows:
        raise ValueError('No rows for ' + str(path))
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def extract_packets(pcap, destination):
    fields = ['frame.number', 'frame.time_epoch', 'frame.len', 'frame.cap_len',
              'tcp.stream', 'ip.src', 'ip.dst', 'tcp.srcport', 'tcp.dstport',
              'tcp.seq', 'tcp.len', 'ip.len', 'tcp.flags', 'tcp.payload']
    command = ['tshark', '-n', '-r', str(pcap), '-o', 'tcp.relative_sequence_numbers:TRUE',
               '-Y', 'tcp.port == 5001', '-T', 'fields', '-E', 'occurrence=f']
    for field in fields:
        command += ['-e', field]
    streams = defaultdict(lambda: {'client': [], 'server': [], 'endpoints': None})
    stats = Counter()
    profile = destination / 'tshark-profile'
    profile.mkdir()
    # sudo 이후 일반 사용자 분석에서 /root 설정을 읽지 않고 기본 분석 설정을 고정한다.
    environment = {**os.environ, 'WIRESHARK_CONFIG_DIR': str(profile.resolve()), 'LC_ALL': 'C'}
    with (destination / 'packets.csv').open('w', newline='') as table, \
            (destination / 'tshark-analysis.log').open('w') as error:
        writer = csv.writer(table)
        writer.writerow(fields[:-1])  # raw payload에는 정답이 있으므로 표에서 제외
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error, text=True,
                                   env=environment)
        try:
            for line in process.stdout:
                row = line.rstrip('\n').split('\t')
                if len(row) != len(fields):
                    raise ValueError('Unexpected TShark field count')
                values = dict(zip(fields, row))
                writer.writerow(row[:-1])
                stats['packets'] += 1
                if int(values['frame.cap_len']) < int(values['frame.len']):
                    raise ValueError('Truncated capture record')
                ip_len = int(values['ip.len'])
                stats['ip_records_over_1500'] += ip_len > 1500
                stats['max_ip_len'] = max(stats['max_ip_len'], ip_len)
                flags = int(values['tcp.flags'], 16)
                stats['syn_records'] += bool(flags & 2)
                stats['fin_records'] += bool(flags & 1)
                stats['rst_records'] += bool(flags & 4)
                from_client = int(values['tcp.dstport']) == 5001
                endpoints = ((values['ip.src'], int(values['tcp.srcport']), values['ip.dst'], 5001)
                             if from_client else
                             (values['ip.dst'], int(values['tcp.dstport']), values['ip.src'], 5001))
                stream = streams[int(values['tcp.stream'])]
                if stream['endpoints'] is not None and stream['endpoints'] != endpoints:
                    raise ValueError('TCP stream endpoint mismatch')
                stream['endpoints'] = endpoints
                payload = bytes.fromhex(values['tcp.payload'].replace(':', ''))
                if len(payload) != int(values['tcp.len']):
                    raise ValueError('Incomplete TCP payload at frame ' + values['frame.number'])
                if payload:
                    packet = Packet(int(values['frame.number']),
                                    int(Decimal(values['frame.time_epoch']) * 1000000000),
                                    int(values['tcp.seq']), ip_len, payload)
                    stream['client' if from_client else 'server'].append(packet)
            if process.wait(timeout=15):
                raise ValueError('TShark analysis failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    if not streams:
        raise ValueError('No TCP streams found')
    return streams, dict(stats), command


def build(run_dir, destination, threshold, legacy=False):
    if threshold <= 0:
        raise ValueError('Elephant threshold must be positive')
    manifest_path = run_dir / 'manifest.json'
    if legacy:
        manifest = dict(run_id=run_dir.name, metadata_byteorder='little',
                        provenance='old one-pair learning capture; parser validation only',
                        pairs=[dict(client='client1', server='server1', client_ip='20.0.1.1',
                                    server_ip='20.0.0.1', requests=10, flows_file='flows.txt')])
    else:
        manifest = json.loads(manifest_path.read_text())
        if manifest['status'] not in ('captured', 'complete', 'dataset_failed'):
            raise ValueError('Capture did not complete successfully')
    if manifest['metadata_byteorder'] not in ('little', 'big'):
        raise ValueError('Unknown generator metadata byte order')
    pcap = run_dir / 'traffic.pcap'
    pcap_hash = digest_file(pcap)
    if manifest.get('pcap_sha256', pcap_hash) != pcap_hash:
        raise ValueError('Pcap checksum changed')
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'processor-source.py').write_bytes(Path(__file__).read_bytes())
    summary = dict(status='building', source_run=str(run_dir), pcap_sha256=pcap_hash,
                   processor_sha256=digest_file(Path(__file__)),
                   unit='TrafficGenerator application request; NOT a TCP connection',
                   label_threshold_bytes=threshold, label_status='exploratory, not professor-approved',
                   split='unassigned; one run is insufficient for independent train/test',
                   source_provenance=manifest.get('provenance', 'new professor-based capture'))
    try:
        streams, packet_stats, command = extract_packets(pcap, destination)
        ground_truth = {}
        for pair in manifest['pairs']:
            rows = [list(map(int, line.split())) for line in (run_dir / pair['flows_file']).read_text().splitlines()]
            if len(rows) != pair['requests'] or any(len(row) != 5 for row in rows):
                raise ValueError('Incomplete client log: cannot infer request IDs from rows')
            ground_truth[(pair['client_ip'], pair['server_ip'])] = (pair, rows)
        samples, features, request_packets, controls, used = [], [], [], 0, set()
        for stream_id, stream in sorted(streams.items()):
            client_ip, client_port, server_ip, server_port = stream['endpoints']
            pair, logs = ground_truth[(client_ip, server_ip)]
            exchanges = decode_exchange(stream['client'], stream['server'], manifest['metadata_byteorder'])
            if not exchanges or exchanges[-1]['request_id'] != 0:
                raise ValueError('Missing generator termination exchange')
            for exchange in exchanges:
                request_id = exchange['request_id']
                if request_id == 0:
                    if exchange['final_bytes'] != 100:
                        raise ValueError('Unexpected control response size')
                    controls += 1
                    continue
                key = (client_ip, server_ip, request_id)
                if key in used or not 1 <= request_id <= len(logs):
                    raise ValueError('Duplicate or unknown request ID')
                used.add(key)
                size, fct_us, dscp, rate, goodput = logs[request_id - 1]
                if (size, dscp << 2, rate) != (exchange['final_bytes'], exchange['tos'], exchange['rate_mbps']):
                    raise ValueError('Pcap and flow log disagree')
                observations = body_observations(stream['server'], exchange['body_seq_start'], exchange['body_seq_end'])
                sample_id = '%s:%s:%d' % (manifest['run_id'], pair['client'], request_id)
                sample = dict(sample_id=sample_id, run_id=manifest['run_id'], client=pair['client'],
                              server=pair['server'], tcp_stream=stream_id, client_ip=client_ip,
                              client_port=client_port, server_ip=server_ip, server_port=server_port,
                              request_id=request_id, final_bytes=size, fct_us=fct_us,
                              label='elephant' if size >= threshold else 'mice',
                              response_body_records=len(observations),
                              first_body_frame=observations[0][0].frame if observations else '',
                              last_body_frame=observations[-1][0].frame if observations else '',
                              body_seq_start=exchange['body_seq_start'], body_seq_end=exchange['body_seq_end'],
                              capture_verified_unique_body_bytes=sum(count for _, count in observations))
                samples.append(sample)
                for packet, count in observations:
                    request_packets.append(dict(sample_id=sample_id, frame=packet.frame,
                                                time_ns=packet.time_ns, new_body_bytes=count,
                                                ip_len=packet.ip_len))
                for n in range(1, 11):
                    features.append(feature_row(sample_id, n, observations))
        if len(samples) != sum(pair['requests'] for pair in manifest['pairs']):
            raise ValueError('Not every logged request has been captured and matched')
        samples.sort(key=lambda row: (row['client'], row['request_id']))
        write_csv(destination / 'requests.csv', samples)
        write_csv(destination / 'features.csv', features)
        write_csv(destination / 'request-packets.csv', request_packets)
        feature_columns = [key for key in features[0] if key != 'sample_id']
        (destination / 'feature-columns.json').write_text(json.dumps(feature_columns, indent=2) + '\n')
        summary.update(status='complete', packets=packet_stats, tcp_streams=len(streams),
                       termination_exchanges=controls, requests=len(samples), feature_rows=len(features),
                       response_bytes=sum(row['final_bytes'] for row in samples),
                       mean_fct_ms=sum(row['fct_us'] for row in samples) / len(samples) / 1000,
                       labels=dict(Counter(row['label'] for row in samples)),
                       unique_request_sizes=len({row['final_bytes'] for row in samples}),
                       tshark_command=command, short_at_n10=sum(row['response_body_records'] < 10 for row in samples))
        card = '''# 요청 단위 데이터셋

- 출처: `{run}`. {provenance}
- 완료된 생성기 요청 {requests}건, N=1..10 특징 {features}행. 10개 N은 같은 요청의 파생 표본이다.
- `requests.csv`: 추적 정보와 정답. final_bytes는 헤더/생성기 메타데이터/종료 제어/중복 재전송을 제외한 본 응답 크기다. pcap 재조립 값과 생성기 로그를 모두 대조했다.
- `features.csv`: sample_id로 정답과 연결한다. 입력은 `feature-columns.json`의 허용 열만 사용한다. raw payload, IP/포트, 요청 ID, 최종 크기/FCT는 입력에서 제외한다.
- 초기 N은 **서버→클라이언트 방향에서 새 응답 본문 바이트를 포함한 캡처 기록**을 센다. SYN/ACK-only 및 제어-only, 완전 중복 재전송은 제외한다. body_bytes는 이 기록의 새 본문 바이트, ip_len은 기록된 IP 전체 길이, iat_ms는 선택한 기록의 도착 간격이다. TCP 재조립용 정렬과 관측 시각 순서는 구분한다.
- N보다 짧으면 0 패딩 + mask/observed_count를 사용한다. 첫 간격은 0이다. 정규화는 수행하지 않았다. 이후 학습 분할에만 전처리를 fit해야 한다.
- Elephant 기준은 임시로 **{threshold} B 이상**이다. 교수님 확정값이 아니다. 연속 정답 final_bytes를 보존하므로 다시 라벨링할 수 있다.
- 한 실행은 독립 학습/평가 분할로 나누지 않았다. 같은 요청의 여러 N, 같은 연결, 동일 seed의 반복 workload가 양쪽에 섞이지 않도록 그룹 단위로 분리해야 한다. 원본 두 client의 seed=1 때문에 크기열도 중복될 수 있다.
- IP 길이 1500 초과 캡처 기록: {oversize}. 큰 기록이 있으면 물리 패킷 단위 초기 특징으로 해석하지 않는다. 원본 캡처와 offload/MTU 기록을 함께 확인한다.
- 캡처 드롭은 pcap/로그가 보고하지 않으면 미상이다. 전체 응답 본문을 복원했다는 사실이 네트워크 무손실을 뜻하지 않는다.
- 최종 TCP 연결 크기 예측용 데이터와 다르다. 요청 경계는 생성기 프로토콜을 해석해서 확보했다. 일반 네트워크에서 같은 관측 경계를 얻는 방식은 별도 연구 사항이다.
'''.format(run=run_dir, provenance=summary['source_provenance'], requests=len(samples),
           features=len(features), threshold=threshold, oversize=packet_stats.get('ip_records_over_1500', 0))
        (destination / 'DATASET.md').write_text(card)
    except BaseException as exc:
        summary.update(status='failed', error=str(exc))
        raise
    finally:
        (destination / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--threshold-bytes', type=int, default=1000000)
    parser.add_argument('--legacy', action='store_true', help='기존 한 쌍 10요청 파일로 추출기 검증')
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    build(run_dir, args.out.resolve() if args.out else run_dir / 'dataset',
          args.threshold_bytes, args.legacy)


if __name__ == '__main__':
    main()
