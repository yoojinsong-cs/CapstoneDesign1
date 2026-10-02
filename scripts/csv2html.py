"""pcap2csv.py output -> HTML table for viewing in a browser.

usage: python3 csv2html.py data/run2_n1000_client1.csv   (writes data/run2_n1000_client1.html)
"""
import sys, csv, html

ELEPHANT = 1_000_000  # bytes; highlight only, not the final label definition


def human(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1000 or unit == 'MB':
            return '%.1f %s' % (n, unit) if unit != 'B' else '%d B' % n
        n /= 1000


src = sys.argv[1]
rows = list(csv.DictReader(open(src)))
N = sum(1 for k in rows[0] if k.startswith('len'))
big = sum(int(r['size']) >= ELEPHANT for r in rows)

head = ['flow_id', 'size', 'size(읽기 쉽게)'] + ['len%d' % k for k in range(1, N + 1)] + ['t%d' % k for k in range(1, N + 1)]
body = []
for r in rows:
    size = int(r['size'])
    cls = ' class=big' if size >= ELEPHANT else ''
    cells = [r['flow_id'], '{:,}'.format(size), human(size)] + [r['len%d' % k] for k in range(1, N + 1)] + [r['t%d' % k] for k in range(1, N + 1)]
    body.append('<tr%s>' % cls + ''.join('<td>%s</td>' % html.escape(c) for c in cells) + '</tr>')

page = '''<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>%(name)s</title>
<style>
body{font-family:sans-serif;margin:16px;color:#222}
.info{max-width:900px;line-height:1.6;font-size:14px}
.info code{background:#f0f0f0;padding:1px 4px;border-radius:3px}
.wrap{overflow:auto;max-height:75vh;border:1px solid #ccc}
table{border-collapse:collapse;font-size:12px;white-space:nowrap}
th,td{border:1px solid #ddd;padding:3px 7px;text-align:right}
th{background:#eee;position:sticky;top:0}
th.lbl,td:nth-child(2),td:nth-child(3){background:#fff5d6}
th.len{background:#e3efff} th.t{background:#e6f7e6}
tr.big td:first-child{background:#ffd0d0;font-weight:bold}
tr:hover td{background:#fffbe0}
</style>
<h2>%(name)s</h2>
<div class=info>
플로우 %(n)d개 · 1MB 이상 %(big)d개 (<span style="background:#ffd0d0">빨간 flow_id</span>)<br>
<b style="background:#fff5d6">size</b> = 플로우 전체 크기(<b>정답</b>, 예측 입력으로 쓰면 안 됨) ·
<b style="background:#e3efff">len<i>k</i></b> = 서버가 보낸 <i>k</i>번째 패킷의 크기(바이트) ·
<b style="background:#e6f7e6">t<i>k</i></b> = 주문서를 보낸 뒤 <i>k</i>번째 패킷이 도착하기까지 걸린 시간(초)<br>
<code>len1</code>은 항상 16: 서버가 데이터를 보내기 전에 받은 주문서를 그대로 돌려보냄(echo). 빈칸 = 플로우가 이미 끝남.
</div><br>
<div class=wrap><table><tr>%(head)s</tr>%(body)s</table></div>
</html>''' % {
    'name': html.escape(src.split('/')[-1]), 'n': len(rows), 'big': big,
    'head': ''.join('<th class=%s>%s</th>' % ('lbl' if h.startswith('size') else 'len' if h.startswith('len') else 't' if h.startswith('t') else '', h) for h in head),
    'body': ''.join(body),
}
out = src.rsplit('.', 1)[0] + '.html'
open(out, 'w').write(page)
print(out)
