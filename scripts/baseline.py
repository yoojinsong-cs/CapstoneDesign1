"""Baseline: guess elephant/mice from the first N data packets with ONE threshold rule.

train = run 1 (data/n1000_*), test = run 2 (data/run2_n1000_*): different runs, so no shared flows.
len1/t1 is the server's 16B echo of the request (same for every flow) -> packets counted from len2.
label: elephant if size >= 1 MB.

usage: python3 baseline.py
"""
import csv, glob

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
    }


def score(pred, true):
    tp = sum(p and t for p, t in zip(pred, true))
    fp = sum(p and not t for p, t in zip(pred, true))
    fn = sum(t and not p for p, t in zip(pred, true))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def best_rule(X, y, name):
    """Try every threshold (both directions) on train, keep the best F1."""
    vals = sorted(set(x[name] for x in X))
    best = (-1, None, None)
    for v in vals:
        for ge in (True, False):
            pred = [(x[name] >= v) if ge else (x[name] <= v) for x in X]
            f1 = score(pred, y)[2]
            if f1 > best[0]:
                best = (f1, v, ge)
    return best[1], best[2]


def apply(X, name, v, ge):
    return [(x[name] >= v) if ge else (x[name] <= v) for x in X]


if __name__ == '__main__':
    train, test = load('data/n1000_client*.csv'), load('data/run2_n1000_client*.csv')
    print('train %d flows (%d elephants) | test %d flows (%d elephants)' % (
        len(train), sum(r[0] for r in train), len(test), sum(r[0] for r in test)))
    print('always-guess-elephant F1 on test: %.2f\n' % score([True] * len(test), [r[0] for r in test])[2])

    print('N | best rule (learned on train)          | test precision recall  F1  | hard-only F1')
    for N in range(1, 10):
        Xtr = [features(l, t, N) for _, l, t in train]
        Xte = [features(l, t, N) for _, l, t in test]
        ytr, yte = [r[0] for r in train], [r[0] for r in test]
        results = []
        for name in Xtr[0]:
            v, ge = best_rule(Xtr, ytr, name)
            results.append((score(apply(Xtr, name, v, ge), ytr)[2], name, v, ge))
        _, name, v, ge = max(results)  # pick the feature by TRAIN F1 only
        p, r, f = score(apply(Xte, name, v, ge), yte)
        # "hard" = flows still sending after N packets (can't be told apart by 'it already ended')
        hard = [i for i, x in enumerate(Xte) if x['pkts_seen'] == N and x['last_pkt_full']]
        hf = score([apply(Xte, name, v, ge)[i] for i in hard], [yte[i] for i in hard])[2]
        rule = '%s %s %s' % (name, '>=' if ge else '<=', round(v, 4))
        print('%d | %-38s | %.2f      %.2f  %.2f | %.2f (%d flows)' % (N, rule, p, r, f, hf, len(hard)))
