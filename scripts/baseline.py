"""Baseline: guess elephant/mice from the first N data packets with ONE threshold rule
(optionally gated by "the flow is still sending", i.e. a depth-2 tree).

train = client1, test = client2 of the same run: independent seeds, so no shared flows.
len1/t1 is the server's echo of the request (same for every flow) -> packets counted from len2.
label: elephant if size >= 1 MB.

usage:
  python3 baseline.py <train.csv> <test.csv>   per-N table for one run
  python3 baseline.py                          F1 per clue level for every data/*_clue*_client1.csv
The output is also saved: data/baseline_all.txt (no args) or <train name without _client1.csv>_baseline.txt.
"""
import atexit, csv, glob, re, sys, unicodedata

ELEPHANT = 1_000_000


def load(pattern):
    rows = []
    for f in sorted(glob.glob(pattern)):
        for r in csv.DictReader(open(f)):
            lens = [int(r['len%d' % k]) for k in range(2, 11) if r['len%d' % k]]
            ts = [float(r['t%d' % k]) for k in range(2, 11) if r['t%d' % k]]
            rows.append((int(r['size']) >= ELEPHANT, lens, ts))
    return rows


def features(lens, ts, N):
    """What a switch could know after watching up to N data packets."""
    lens, ts = lens[:N], ts[:N]
    return {
        'pkts_seen': len(lens),                      # fewer than N -> flow already ended
        'bytes_seen': sum(lens),
        'last_pkt_full': int(bool(lens) and lens[-1] >= 1448),  # a short last packet = flow finished
        'time_to_Nth': ts[-1] if len(ts) == N else 99.0,        # how long N packets took
        'first_full': int(bool(lens) and lens[0] >= 1448),      # bulk starts full, login/stream start small
        'first_gap': ts[1] - ts[0] if len(ts) >= 2 else 0.0,    # stream pauses after its header
    }


def still_sending(x, N):
    return x['pkts_seen'] == N and x['last_pkt_full']


def score(pred, true):
    tp = sum(p and t for p, t in zip(pred, true))
    fp = sum(p and not t for p, t in zip(pred, true))
    fn = sum(t and not p for p, t in zip(pred, true))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def apply(X, N, rule):
    name, v, ge, gated = rule
    return [(not gated or still_sending(x, N)) and ((x[name] >= v) if ge else (x[name] <= v)) for x in X]


def best_rule(X, y, N):
    """Try every feature, threshold, direction, with/without the still-sending gate; keep the best train F1."""
    best = (-1, None)
    for name in X[0]:
        for v in sorted(set(x[name] for x in X)):
            for ge in (True, False):
                for gated in (False, True):
                    f1 = score(apply(X, N, (name, v, ge, gated)), y)[2]
                    if f1 > best[0]:
                        best = (f1, (name, v, ge, gated))
    return best[1]


def test_f1(train, test, N):
    rule = best_rule([features(l, t, N) for _, l, t in train], [r[0] for r in train], N)
    return score(apply([features(l, t, N) for _, l, t in test], N, rule), [r[0] for r in test]), rule


NAMES = {  # feature -> how it reads in the report
    'pkts_seen': '받은 패킷 수', 'bytes_seen': '받은 바이트', 'last_pkt_full': '마지막 패킷이 꽉 참',
    'time_to_Nth': 'N번째 패킷까지 걸린 시간(초)', 'first_full': '첫 패킷이 꽉 참', 'first_gap': '첫-둘째 패킷 간격',
}


def describe(rule):
    name, v, ge, gated = rule
    value = '%.1fms' % (v * 1000) if name == 'first_gap' else '%g' % round(v, 4)
    return '%s%s %s %s' % ('아직 보내는 중 & ' if gated else '', NAMES[name], '>=' if ge else '<=', value)


def pad(text, width):
    """Left-align to a screen width (Korean letters take two columns)."""
    return text + ' ' * (width - sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text))


def row(cells, widths):
    print(' | '.join(pad(c, w) for c, w in zip(cells, widths)).rstrip())


def sweep():
    runs = sorted(glob.glob('data/*_clue*_client1.csv'), key=lambda f: int(re.search(r'clue(\d+)', f).group(1)))
    print('단서 강도별 F1 점수 (첫 N개 패킷만 보고 elephant 예측, 1에 가까울수록 잘 맞힘)')
    print('학습 = client1, 시험 = client2. 괄호 = elephant 수. 무조건 elephant라고 찍으면 약 0.17\n')
    widths = [9, 4, 4, 4, 4, 22]
    row(['단서 강도', 'N=1', 'N=3', 'N=5', 'N=9', '흐름 수 학습 / 시험', '실험'], widths + [0])
    print('-' * 90)
    for f in runs:
        train, test = load(f), load(f.replace('client1', 'client2'))
        f1s = ['%.2f' % test_f1(train, test, N)[0][2] for N in (1, 3, 5, 9)]
        clue = int(re.search(r'clue(\d+)', f).group(1)) / 100
        counts = '%d (%d) / %d (%d)' % (len(train), sum(r[0] for r in train), len(test), sum(r[0] for r in test))
        row(['%.2f' % clue] + f1s + [counts, f.replace('_client1.csv', '')], widths + [0])


class Tee:
    """stdout that also writes to a file."""
    def __init__(self, path):
        self.path, self.f = path, open(path, 'w')
    def write(self, s):
        sys.__stdout__.write(s)
        self.f.write(s)
    def flush(self):
        sys.__stdout__.flush()
        self.f.flush()


if __name__ == '__main__':
    out = 'data/baseline_all.txt' if len(sys.argv) < 3 else re.sub(r'(_client1)?\.csv$', '', sys.argv[1]) + '_baseline.txt'
    sys.stdout = Tee(out)
    atexit.register(lambda: sys.__stdout__.write('\nsaved %s\n' % out))
    if len(sys.argv) < 3:
        sweep()
        sys.exit()
    train, test = load(sys.argv[1]), load(sys.argv[2])
    print('실험: %s (학습) / %s (시험)' % (sys.argv[1], sys.argv[2]))
    print('흐름 수: 학습 %d개 (elephant %d개), 시험 %d개 (elephant %d개)' % (
        len(train), sum(r[0] for r in train), len(test), sum(r[0] for r in test)))
    print('찍기 기준선 (전부 elephant라고 예측) F1: %.2f\n' % score([True] * len(test), [r[0] for r in test])[2])
    print('정밀도 = elephant라고 한 것 중 진짜 비율, 재현율 = 진짜 elephant 중 잡아낸 비율, F1 = 둘을 합친 점수')
    print('안 끝난 흐름 F1 = N개 패킷 뒤에도 아직 보내는 흐름만 따로 채점 (괄호 = 그 흐름 수)\n')

    widths = [2, 6, 6, 4, 20]
    row(['N', '정밀도', '재현율', 'F1', '안 끝난 흐름 F1', '학습 데이터로 고른 규칙'], widths + [0])
    print('-' * 90)
    for N in range(1, 10):
        (p, r, f), rule = test_f1(train, test, N)
        Xte, yte = [features(l, t, N) for _, l, t in test], [r[0] for r in test]
        # "hard" = flows still sending after N packets (can't be told apart by 'it already ended')
        hard = [i for i, x in enumerate(Xte) if still_sending(x, N)]
        pred = apply(Xte, N, rule)
        hf = score([pred[i] for i in hard], [yte[i] for i in hard])[2]
        row([str(N), '%.2f' % p, '%.2f' % r, '%.2f' % f, '%.2f (%d)' % (hf, len(hard)), describe(rule)], widths + [0])
