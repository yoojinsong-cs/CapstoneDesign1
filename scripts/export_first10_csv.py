"""Export the first server-to-client packets of every flow from a PCAP.

The output format matches pcap2csv.py, but tshark output is consumed as a
stream so multi-gigabyte captures do not have to be loaded into memory.

usage: python3 export_first10_csv.py <pcap> <output.csv> [N=10] [port=5001]
"""

import csv
import os
import subprocess
import sys


def tshark_rows(pcap, display_filter, fields):
    command = [
        'tshark', '-n', '-r', pcap, '-Y', display_filter,
        '-T', 'fields', '-E', 'separator=\t', '-E', 'occurrence=f',
    ]
    for field in fields:
        command.extend(['-e', field])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1024 * 1024,
    )
    try:
        for line in process.stdout:
            yield line.rstrip('\n').split('\t')
    finally:
        process.stdout.close()

    stderr = process.stderr.read()
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            'tshark exited with status %d: %s' % (return_code, stderr.strip()))


def export(pcap, output_path, packet_limit=10, port=5001):
    starts = {}
    seen_flow_ids = set()
    flows = {}

    request_filter = 'tcp.dstport==%d && tcp.len==16' % port
    request_fields = [
        'frame.number', 'frame.time_epoch', 'tcp.stream',
        'tcp.ack_raw', 'tcp.payload',
    ]
    for row in tshark_rows(pcap, request_filter, request_fields):
        if len(row) != len(request_fields):
            continue
        frame_number, timestamp, stream, ack_raw, payload = row
        try:
            metadata = bytes.fromhex(payload)
            if len(metadata) < 16 or not ack_raw:
                continue
            flow_id = int.from_bytes(metadata[0:4], 'little')
            size = int.from_bytes(metadata[4:8], 'little')
            response_start = int(ack_raw)
        except ValueError:
            continue

        if flow_id != 0 and flow_id in seen_flow_ids:
            continue
        if flow_id != 0:
            seen_flow_ids.add(flow_id)

        starts[int(frame_number)] = (stream, flow_id, size, float(timestamp))
        if flow_id != 0:
            flows[flow_id] = {
                'pcap': pcap,
                'flow_id': flow_id,
                'size': size,
                't0': float(timestamp),
                'response_start': response_start,
                'packets': [],
            }

    current = {}
    packet_filter = 'tcp.port==%d && tcp.len>0' % port
    packet_fields = [
        'frame.number', 'frame.time_epoch', 'tcp.stream',
        'tcp.srcport', 'tcp.seq_raw', 'tcp.len',
    ]
    server_packet_count = 0
    for row in tshark_rows(pcap, packet_filter, packet_fields):
        if len(row) != len(packet_fields):
            continue
        frame_number, timestamp, stream, source_port, sequence, tcp_length = row
        try:
            frame_number = int(frame_number)
            timestamp = float(timestamp)
            source_port = int(source_port)
            sequence = int(sequence)
            tcp_length = int(tcp_length)
        except ValueError:
            continue

        if frame_number in starts:
            request_stream, flow_id, _, _ = starts[frame_number]
            if flow_id != 0:
                current[request_stream] = flows[flow_id]
        elif source_port == port and stream in current:
            flow = current[stream]
            sequence_offset = (
                sequence - flow['response_start']) % (1 << 32)
            response_length = flow['size'] + 16
            if (sequence_offset < response_length and
                    len(flow['packets']) < packet_limit):
                flow['packets'].append((tcp_length, timestamp - flow['t0']))
            server_packet_count += 1
            if server_packet_count % 1_000_000 == 0:
                print(
                    '%s: processed %d server payload packets' %
                    (pcap, server_packet_count),
                    flush=True,
                )

    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = output_path + '.tmp'
    header = (
        ['pcap', 'flow_id', 'size'] +
        ['len%d' % index for index in range(1, packet_limit + 1)] +
        ['t%d' % index for index in range(1, packet_limit + 1)]
    )
    try:
        with open(temporary_path, 'w', newline='') as output_file:
            writer = csv.writer(output_file)
            writer.writerow(header)
            for flow_id in sorted(flows):
                flow = flows[flow_id]
                lengths = [packet[0] for packet in flow['packets']]
                times = ['%.6f' % packet[1] for packet in flow['packets']]
                lengths.extend([''] * (packet_limit - len(lengths)))
                times.extend([''] * (packet_limit - len(times)))
                writer.writerow(
                    [flow['pcap'], flow_id, flow['size']] + lengths + times)
        os.replace(temporary_path, output_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)

    print(
        '%s: wrote %d flows to %s' % (pcap, len(flows), output_path),
        flush=True,
    )


def main():
    if len(sys.argv) < 3 or len(sys.argv) > 5:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    pcap = sys.argv[1]
    output_path = sys.argv[2]
    packet_limit = int(sys.argv[3]) if len(sys.argv) >= 4 else 10
    port = int(sys.argv[4]) if len(sys.argv) >= 5 else 5001
    export(pcap, output_path, packet_limit, port)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
