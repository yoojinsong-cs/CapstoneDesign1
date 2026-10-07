"""Show how a pcap splits into flows (same rule as pcap2csv.py: each request starts a new flow).

usage:
  python3 show_flows.py <pcap>                  connections -> flows tree (first 10 connections)
  python3 show_flows.py <pcap> <stream>         every data packet of one connection, grouped by flow
  python3 show_flows.py <pcap> <stream> <port>  (server port, default 5001)
"""
import sys
from pcap2csv import tshark

def main(pcap, stream=None, port=5001):
    flt = 'tcp.port==%d && tcp.len>0' % port + ('' if stream is None else ' && tcp.stream==%s' % stream)
    pkts = tshark(pcap, flt, ['frame.number', 'frame.time_relative', 'tcp.stream', 'tcp.srcport', 'tcp.len', 'tcp.payload'])

    seen = set()
    conns = {}  # stream -> list of flows; a flow = (id, size, packets)
    for fn, t, st, sport, ln, payload in pkts:
        if int(sport) != port and int(ln) == 16:  # request from client
            b = bytes.fromhex(payload)
            fid = int.from_bytes(b[0:4], 'little')
            if fid == 0 or fid in seen:  # close message / retransmitted request
                continue
            seen.add(fid)
            conns.setdefault(st, []).append((fid, int.from_bytes(b[4:8], 'little'), []))
            conns[st][-1][2].append((fn, t, 'request', ln))
        elif int(sport) == port and conns.get(st):  # server data -> current flow of this connection
            conns[st][-1][2].append((fn, t, 'server', ln))

    if stream is None:
        for st in list(conns)[:10]:
            print('connection (tcp.stream) %s' % st)
            for fid, size, ps in conns[st]:
                print('  ├─ flow %-5d %13s B  %d server packets' % (fid, format(size, ','), len(ps) - 1))
        print('\n%d connections, %d flows' % (len(conns), sum(len(v) for v in conns.values())))
        return

    for fid, size, ps in conns.get(stream, []):
        print('── flow %d  (size %s B) ──' % (fid, format(size, ',')))
        print('  %-7s %-12s %-8s %s' % ('frame', 'time(s)', 'from', 'len'))
        for fn, t, who, ln in ps:
            print('  %-7s %-12.6f %-8s %s' % (fn, float(t), who, ln))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, int(sys.argv[3]) if len(sys.argv) > 3 else 5001)
