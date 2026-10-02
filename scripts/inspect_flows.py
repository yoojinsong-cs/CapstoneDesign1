"""Quick look at pcap2csv.py output: sanity checks + example flows per app type.

usage: python3 inspect_flows.py data/1002_VL2_3apps_100flows_client1.csv [more CSVs ...]
"""
import sys, csv, collections

ELEPHANT = 1_000_000
APPS = {'0': 'bulk (파일형)', '1': 'login (로그인형)', '2': 'stream (영상형)'}

rows = [r for f in sys.argv[1:] for r in csv.DictReader(open(f))]
N = sum(1 for k in rows[0] if k.startswith('len'))
lens = lambda r: [int(r['len%d' % k]) for k in range(1, N + 1) if r['len%d' % k]]
times = lambda r: [float(r['t%d' % k]) for k in range(1, N + 1) if r['t%d' % k]]

print('flows', len(rows), '| elephants (>=1MB)', sum(int(r['size']) >= ELEPHANT for r in rows))
print('len1 values (request echo, 20 = new server):', dict(collections.Counter(lens(r)[0] for r in rows)))
print('largest data packet (1448 = GRO off):', max(max(lens(r)[1:] or [0]) for r in rows))
short = [r['flow_id'] for r in rows if int(r['size']) > N * 1448 and len(lens(r)) < N]
print('big flows with fewer than %d packets (should be 0):' % N, short or 0)

for a, name in APPS.items():
    rs = [r for r in rows if r.get('app', '0') == a]
    if not rs:
        continue
    print('\n%s: %d flows, elephants %d' % (name, len(rs), sum(int(r['size']) >= ELEPHANT for r in rs)))
    for r in rs[:5]:
        print('  flow %4s  size %11s  len %s' % (r['flow_id'], r['size'], lens(r)[1:]))
        print('  %29s  t   %s' % ('', ['%.3f' % t for t in times(r)[1:]]))
