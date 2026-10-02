"""pcap -> CSV: one row per flow = label (size) + first N server->client data packets.

A flow starts with the client's request {id, size, tos, rate, app} (little-endian, 20 bytes;
older pcaps have the 16-byte version without app -> app 0).
id == 0 is the close message. Retransmitted requests are skipped.
`size` is the LABEL and `app` (0 bulk, 1 login, 2 stream) is for analysis only -- never use either as a feature.

usage: python3 pcap2csv.py <pcap> [N=10] [port=5001] > out.csv
"""
import sys, csv, subprocess


def tshark(pcap, flt, fields):
    cmd = ['tshark', '-r', pcap, '-Y', flt, '-T', 'fields', '-E', 'separator=\t']
    for f in fields:
        cmd += ['-e', f]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [line.split('\t') for line in out.splitlines()]


def flows(pcap, N=10, port=5001):
    # pass 1: requests (only these need the payload)
    starts = {}  # frame number -> (stream, id, size, app)
    seen = set()
    for fn, stream, payload in tshark(pcap, 'tcp.dstport==%d && (tcp.len==16 || tcp.len==20)' % port,
                                      ['frame.number', 'tcp.stream', 'tcp.payload']):
        b = bytes.fromhex(payload)
        fid = int.from_bytes(b[0:4], 'little')
        if fid in seen and fid != 0:  # retransmitted request
            continue
        seen.add(fid)
        starts[int(fn)] = (stream, fid, int.from_bytes(b[4:8], 'little'), int.from_bytes(b[16:20], 'little'))

    # pass 2: walk all packets in order, assign data packets to the current flow of their stream
    current = {}  # stream -> flow dict
    result = []
    for fn, t, stream, sport, ln in tshark(pcap, 'tcp.port==%d && tcp.len>0' % port,
                                           ['frame.number', 'frame.time_epoch', 'tcp.stream', 'tcp.srcport', 'tcp.len']):
        fn, t = int(fn), float(t)
        if fn in starts:
            _, fid, size, app = starts[fn]
            if fid == 0:  # close is sent right after the last request, while data is still flowing -> ignore
                continue
            f = {'pcap': pcap, 'flow_id': fid, 'size': size, 'app': app, 't0': t, 'pkts': []}
            current[stream] = f
            result.append(f)
        elif int(sport) == port and stream in current:
            f = current[stream]
            if len(f['pkts']) < N:
                f['pkts'].append((int(ln), t - f['t0']))
    return result


if __name__ == '__main__':
    pcap = sys.argv[1]
    N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 5001

    w = csv.writer(sys.stdout)
    w.writerow(['pcap', 'flow_id', 'size', 'app'] + ['len%d' % k for k in range(1, N + 1)] + ['t%d' % k for k in range(1, N + 1)])
    for f in flows(pcap, N, port):
        lens = [p[0] for p in f['pkts']] + [''] * (N - len(f['pkts']))
        ts = ['%.6f' % p[1] for p in f['pkts']] + [''] * (N - len(f['pkts']))
        w.writerow([f['pcap'], f['flow_id'], f['size'], f['app']] + lens + ts)
