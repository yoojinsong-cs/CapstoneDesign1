"""Quick look at pcap2csv.py output: sanity checks + example flows.

usage: python3 inspect_flows.py data/<run>_client1.csv [more CSVs ...]
"""
import sys, csv, collections

ELEPHANT = 1_000_000

rows = [r for f in sys.argv[1:] for r in csv.DictReader(open(f))]
N = sum(1 for k in rows[0] if k.startswith('len'))
lens = lambda r: [int(r['len%d' % k]) for k in range(1, N + 1) if r['len%d' % k]]
times = lambda r: [float(r['t%d' % k]) for k in range(1, N + 1) if r['t%d' % k]]

print('flows', len(rows), '| elephants (>=1MB)', sum(int(r['size']) >= ELEPHANT for r in rows))
print('len1 values (request echo, 16 = original TrafficGenerator):',
      dict(collections.Counter((lens(r) or ['missing'])[0] for r in rows)))
print('largest data packet (1448 = GRO off):',
      max((max(lens(r)[1:] or [0]) for r in rows), default=0))
short = [r['flow_id'] for r in rows if int(r['size']) > N * 1448 and len(lens(r)) < N]
print('big flows with fewer than %d packets (should be 0):' % N, short or 0)

print('\nexample flows:')
for r in rows[:10]:
    print('  flow %4s  size %11s  len %s' % (r['flow_id'], r['size'], lens(r)[1:]))
    print('  %29s  t   %s' % ('', ['%.3f' % t for t in times(r)[1:]]))
