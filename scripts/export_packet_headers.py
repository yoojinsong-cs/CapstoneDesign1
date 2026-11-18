"""Export TCP/IP headers from a TrafficGenerator PCAP to packet-level CSV.

Each CSV row is one captured TCP packet.  Packets are associated with the
logical TrafficGenerator flow when its 16-byte request metadata is available.
The payload itself is not written to the CSV.

usage:
  python3 export_packet_headers.py INPUT.pcap OUTPUT.csv [--port 5001]
  python3 export_packet_headers.py INPUT.pcap OUTPUT.csv --stream 0
"""

import argparse
import csv
import os
import subprocess


UINT32_MODULUS = 1 << 32

TSHARK_FIELDS = [
    'frame.number',
    'frame.time_epoch',
    'frame.time_relative',
    'frame.len',
    'frame.cap_len',
    'tcp.stream',
    'eth.src',
    'eth.dst',
    'ip.src',
    'ip.dst',
    'ip.hdr_len',
    'ip.len',
    'ip.dsfield.dscp',
    'ip.dsfield.ecn',
    'ip.id',
    'ip.flags.df',
    'ip.flags.mf',
    'ip.frag_offset',
    'ip.ttl',
    'ip.checksum',
    'ip.checksum.status',
    'tcp.srcport',
    'tcp.dstport',
    'tcp.hdr_len',
    'tcp.len',
    'tcp.seq_raw',
    'tcp.ack_raw',
    'tcp.flags.fin',
    'tcp.flags.syn',
    'tcp.flags.reset',
    'tcp.flags.push',
    'tcp.flags.ack',
    'tcp.flags.ecn',
    'tcp.flags.cwr',
    'tcp.window_size_value',
    'tcp.window_size',
    'tcp.checksum',
    'tcp.checksum.status',
    'tcp.options.mss_val',
    'tcp.options.wscale.shift',
    'tcp.options.sack_perm',
    'tcp.options.timestamp.tsval',
    'tcp.options.timestamp.tsecr',
    'tcp.analysis.ack_rtt',
    'tcp.analysis.retransmission',
    'tcp.analysis.fast_retransmission',
    'tcp.analysis.spurious_retransmission',
    'tcp.analysis.out_of_order',
    'tcp.analysis.duplicate_ack',
    'tcp.payload',
]

OUTPUT_FIELDS = [
    'pcap',
    'frame',
    'time_epoch',
    'time_relative',
    'time_from_request',
    'frame_len',
    'captured_len',
    'tcp_stream',
    'direction',
    'packet_kind',
    'flow_id',
    'flow_size',
    'flow_tos',
    'flow_rate',
    'eth_src',
    'eth_dst',
    'ip_src',
    'ip_dst',
    'ip_header_len',
    'ip_total_len',
    'ip_dscp',
    'ip_ecn',
    'ip_ecn_name',
    'ip_id',
    'ip_df',
    'ip_mf',
    'ip_fragment_offset',
    'ip_ttl',
    'ip_checksum',
    'ip_checksum_status',
    'tcp_srcport',
    'tcp_dstport',
    'tcp_header_len',
    'tcp_payload_len',
    'tcp_seq_raw',
    'tcp_ack_raw',
    'tcp_fin',
    'tcp_syn',
    'tcp_rst',
    'tcp_psh',
    'tcp_ack_flag',
    'tcp_ece',
    'tcp_cwr',
    'tcp_window_raw',
    'tcp_window_scaled',
    'tcp_checksum',
    'tcp_checksum_status',
    'tcp_mss',
    'tcp_window_scale_shift',
    'tcp_sack_permitted',
    'tcp_timestamp_value',
    'tcp_timestamp_echo',
    'tcp_ack_rtt',
    'tcp_retransmission',
    'tcp_fast_retransmission',
    'tcp_spurious_retransmission',
    'tcp_out_of_order',
    'tcp_duplicate_ack',
]

ECN_NAMES = {
    '0': 'Not-ECT',
    '1': 'ECT(1)',
    '2': 'ECT(0)',
    '3': 'CE',
}


def tshark_rows(pcap, display_filter):
    command = [
        'tshark', '-n', '-r', pcap, '-Y', display_filter,
        '-T', 'fields', '-E', 'separator=\t', '-E', 'occurrence=f',
    ]
    for field in TSHARK_FIELDS:
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
            values = line.rstrip('\n').split('\t')
            values.extend([''] * (len(TSHARK_FIELDS) - len(values)))
            yield dict(zip(TSHARK_FIELDS, values))
    finally:
        process.stdout.close()

    stderr = process.stderr.read()
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            'tshark exited with status %d: %s' %
            (return_code, stderr.strip()))


def parse_metadata(payload):
    try:
        raw = bytes.fromhex(payload)
    except ValueError:
        return None
    if len(raw) != 16:
        return None
    return {
        'flow_id': int.from_bytes(raw[0:4], 'little'),
        'flow_size': int.from_bytes(raw[4:8], 'little'),
        'flow_tos': int.from_bytes(raw[8:12], 'little'),
        'flow_rate': int.from_bytes(raw[12:16], 'little'),
    }


def sequence_in_response(sequence, flow, include_end=False):
    offset = (sequence - flow['response_start']) % UINT32_MODULUS
    response_length = flow['flow_size'] + 16
    if include_end:
        return offset <= response_length
    return offset < response_length


def find_flow(flows, direction, sequence, acknowledgment):
    for flow in reversed(flows):
        if direction == 'server_to_client' and sequence is not None:
            if sequence_in_response(sequence, flow):
                return flow
        elif direction == 'client_to_server' and acknowledgment is not None:
            if sequence_in_response(acknowledgment, flow, include_end=True):
                return flow
    return None


def flag_is_set(row, field):
    return row[field] not in ('', '0', 'False', 'false')


def packet_kind(row, direction, metadata, metadata_is_known):
    if flag_is_set(row, 'tcp.flags.reset'):
        return 'rst'
    if flag_is_set(row, 'tcp.flags.syn'):
        return 'syn_ack' if flag_is_set(row, 'tcp.flags.ack') else 'syn'
    if flag_is_set(row, 'tcp.flags.fin'):
        return 'fin'
    if metadata is not None:
        if direction == 'client_to_server':
            return 'close_metadata' if metadata['flow_id'] == 0 else 'request_metadata'
        if metadata_is_known:
            return 'response_metadata'
    if row['tcp.len'] not in ('', '0'):
        return 'data'
    return 'ack'


def int_or_none(value):
    try:
        return int(value, 0)
    except (TypeError, ValueError):
        return None


def export_headers(pcap, output_path, port=5001, stream=None):
    display_filter = 'tcp.port==%d' % port
    if stream is not None:
        display_filter += ' && tcp.stream==%d' % stream

    flows_by_stream = {}
    requests_by_stream_and_id = {}
    directory = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = output_path + '.tmp'
    packet_count = 0

    try:
        with open(temporary_path, 'w', newline='') as output_file:
            writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()

            for row in tshark_rows(pcap, display_filter):
                stream_id = int_or_none(row['tcp.stream'])
                source_port = int_or_none(row['tcp.srcport'])
                destination_port = int_or_none(row['tcp.dstport'])
                sequence = int_or_none(row['tcp.seq_raw'])
                acknowledgment = int_or_none(row['tcp.ack_raw'])
                timestamp = float(row['frame.time_epoch'])

                if destination_port == port:
                    direction = 'client_to_server'
                elif source_port == port:
                    direction = 'server_to_client'
                else:
                    direction = 'unknown'

                metadata = None
                if row['tcp.len'] == '16':
                    metadata = parse_metadata(row['tcp.payload'])

                flows = flows_by_stream.setdefault(stream_id, [])
                known_requests = requests_by_stream_and_id.setdefault(
                    stream_id, {})
                metadata_is_known = False
                flow = None

                if metadata is not None and direction == 'client_to_server':
                    if metadata['flow_id'] != 0:
                        existing = known_requests.get(metadata['flow_id'])
                        if existing is None:
                            flow = dict(metadata)
                            flow['request_time'] = timestamp
                            flow['response_start'] = acknowledgment
                            flows.append(flow)
                            known_requests[metadata['flow_id']] = flow
                        else:
                            flow = existing
                    else:
                        flow = dict(metadata)
                        flow['request_time'] = timestamp
                elif metadata is not None and direction == 'server_to_client':
                    flow = known_requests.get(metadata['flow_id'])
                    metadata_is_known = flow is not None

                if flow is None:
                    flow = find_flow(
                        flows, direction, sequence, acknowledgment)

                output = {
                    'pcap': pcap,
                    'frame': row['frame.number'],
                    'time_epoch': row['frame.time_epoch'],
                    'time_relative': row['frame.time_relative'],
                    'time_from_request': '',
                    'frame_len': row['frame.len'],
                    'captured_len': row['frame.cap_len'],
                    'tcp_stream': row['tcp.stream'],
                    'direction': direction,
                    'packet_kind': packet_kind(
                        row, direction, metadata, metadata_is_known),
                    'flow_id': '',
                    'flow_size': '',
                    'flow_tos': '',
                    'flow_rate': '',
                    'eth_src': row['eth.src'],
                    'eth_dst': row['eth.dst'],
                    'ip_src': row['ip.src'],
                    'ip_dst': row['ip.dst'],
                    'ip_header_len': row['ip.hdr_len'],
                    'ip_total_len': row['ip.len'],
                    'ip_dscp': row['ip.dsfield.dscp'],
                    'ip_ecn': row['ip.dsfield.ecn'],
                    'ip_ecn_name': ECN_NAMES.get(row['ip.dsfield.ecn'], ''),
                    'ip_id': row['ip.id'],
                    'ip_df': row['ip.flags.df'],
                    'ip_mf': row['ip.flags.mf'],
                    'ip_fragment_offset': row['ip.frag_offset'],
                    'ip_ttl': row['ip.ttl'],
                    'ip_checksum': row['ip.checksum'],
                    'ip_checksum_status': row['ip.checksum.status'],
                    'tcp_srcport': row['tcp.srcport'],
                    'tcp_dstport': row['tcp.dstport'],
                    'tcp_header_len': row['tcp.hdr_len'],
                    'tcp_payload_len': row['tcp.len'],
                    'tcp_seq_raw': row['tcp.seq_raw'],
                    'tcp_ack_raw': row['tcp.ack_raw'],
                    'tcp_fin': row['tcp.flags.fin'],
                    'tcp_syn': row['tcp.flags.syn'],
                    'tcp_rst': row['tcp.flags.reset'],
                    'tcp_psh': row['tcp.flags.push'],
                    'tcp_ack_flag': row['tcp.flags.ack'],
                    'tcp_ece': row['tcp.flags.ecn'],
                    'tcp_cwr': row['tcp.flags.cwr'],
                    'tcp_window_raw': row['tcp.window_size_value'],
                    'tcp_window_scaled': row['tcp.window_size'],
                    'tcp_checksum': row['tcp.checksum'],
                    'tcp_checksum_status': row['tcp.checksum.status'],
                    'tcp_mss': row['tcp.options.mss_val'],
                    'tcp_window_scale_shift': row['tcp.options.wscale.shift'],
                    'tcp_sack_permitted': row['tcp.options.sack_perm'],
                    'tcp_timestamp_value': row['tcp.options.timestamp.tsval'],
                    'tcp_timestamp_echo': row['tcp.options.timestamp.tsecr'],
                    'tcp_ack_rtt': row['tcp.analysis.ack_rtt'],
                    'tcp_retransmission': row['tcp.analysis.retransmission'],
                    'tcp_fast_retransmission': row['tcp.analysis.fast_retransmission'],
                    'tcp_spurious_retransmission': row['tcp.analysis.spurious_retransmission'],
                    'tcp_out_of_order': row['tcp.analysis.out_of_order'],
                    'tcp_duplicate_ack': row['tcp.analysis.duplicate_ack'],
                }

                if flow is not None:
                    output['flow_id'] = flow['flow_id']
                    output['flow_size'] = flow['flow_size']
                    output['flow_tos'] = flow['flow_tos']
                    output['flow_rate'] = flow['flow_rate']
                    output['time_from_request'] = '%.9f' % (
                        timestamp - flow['request_time'])

                writer.writerow(output)
                packet_count += 1
                if packet_count % 1_000_000 == 0:
                    print(
                        '%s: exported %d packets' % (pcap, packet_count),
                        flush=True,
                    )

        os.replace(temporary_path, output_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)

    print(
        '%s: wrote %d packet headers to %s' %
        (pcap, packet_count, output_path),
        flush=True,
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pcap', help='input PCAP file')
    parser.add_argument('output', help='output CSV file')
    parser.add_argument('--port', type=int, default=5001,
                        help='TrafficGenerator server port (default: 5001)')
    parser.add_argument('--stream', type=int,
                        help='export only this tcp.stream')
    return parser.parse_args()


def main():
    args = parse_args()
    export_headers(args.pcap, args.output, args.port, args.stream)


if __name__ == '__main__':
    main()
