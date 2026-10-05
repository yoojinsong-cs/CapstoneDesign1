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
import atexit, csv, glob, re, sys

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


def sweep():
    runs = sorted(glob.glob('data/*_clue*_client1.csv'))
    print('clue | flows (elephants) train/test | test F1 at N=1   3   5   9')
    for f in runs:
        train, test = load(f), load(f.replace('client1', 'client2'))
        f1s = ['%.2f' % test_f1(train, test, N)[0][2] for N in (1, 3, 5, 9)]
        clue = int(re.search(r'clue(\d+)', f).group(1)) / 100
        print('%.2f | %d (%d) / %d (%d) | %s   <- %s' % (clue, len(train), sum(r[0] for r in train),
              len(test), sum(r[0] for r in test), '  '.join(f1s), f.replace('_client1.csv', '')))


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
    print('train %d flows (%d elephants) | test %d flows (%d elephants)' % (
        len(train), sum(r[0] for r in train), len(test), sum(r[0] for r in test)))
    print('always-guess-elephant F1 on test: %.2f\n' % score([True] * len(test), [r[0] for r in test])[2])

    print('N | best rule (learned on train)                    | test precision recall  F1  | hard-only F1')
    for N in range(1, 10):
        (p, r, f), rule = test_f1(train, test, N)
        Xte, yte = [features(l, t, N) for _, l, t in test], [r[0] for r in test]
        # "hard" = flows still sending after N packets (can't be told apart by 'it already ended')
        hard = [i for i, x in enumerate(Xte) if still_sending(x, N)]
        pred = apply(Xte, N, rule)
        hf = score([pred[i] for i in hard], [yte[i] for i in hard])[2]
        name, v, ge, gated = rule
        desc = '%s%s %s %s' % ('still sending & ' if gated else '', name, '>=' if ge else '<=', round(v, 4))
        print('%d | %-48s | %.2f      %.2f  %.2f | %.2f (%d flows)' % (N, desc, p, r, f, hf, len(hard)))
