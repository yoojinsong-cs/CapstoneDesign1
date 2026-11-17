"""Random Forest classifier for mice/elephant flow classification.
   Input: CSV produced by pcap2csv.py
   Usage: python3 train_rf.py <csv_file> [elephant_threshold=1000000] [N=10]

   Features: per-flow statistics derived from len1..lenN and t1..tN
   Label:    0 = mice (size <= threshold), 1 = elephant (size > threshold)
   NOTE: 'size' and 'app' columns are NEVER used as features (size is the label, app leaks ground truth)
"""

import sys
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import (confusion_matrix, classification_report,
                             precision_score, recall_score, f1_score)

# ── parameters ────────────────────────────────────────────────────────────────
csv_file           = sys.argv[1]
ELEPHANT_THRESHOLD = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000  # 1 MB
N                  = int(sys.argv[3]) if len(sys.argv) > 3 else 10          # packets used

# ── load data ─────────────────────────────────────────────────────────────────
df = pd.read_csv(csv_file)

len_cols = ['len%d' % k for k in range(1, N + 1)]
t_cols   = ['t%d'   % k for k in range(1, N + 1)]

# keep only rows that have at least 2 data packets (need t2-t1 at minimum)
df = df.dropna(subset=['len1', 'len2', 't1', 't2'])
df[len_cols] = df[len_cols].apply(pd.to_numeric, errors='coerce')
df[t_cols]   = df[t_cols].apply(pd.to_numeric,   errors='coerce')

# ── label ─────────────────────────────────────────────────────────────────────
df['label'] = (df['size'] > ELEPHANT_THRESHOLD).astype(int)
# 0 = mice, 1 = elephant

# ── feature engineering ───────────────────────────────────────────────────────
feats = pd.DataFrame()

# packet-size features
lens = df[len_cols].values
feats['len_first']  = lens[:, 0]
feats['len_mean']   = np.nanmean(lens, axis=1)
feats['len_std']    = np.nanstd(lens,  axis=1)
feats['len_max']    = np.nanmax(lens,  axis=1)
feats['len_sum']    = np.nansum(lens,  axis=1)   # total bytes in first N pkts

# inter-arrival time features (IAT = t[k] - t[k-1])
ts = df[t_cols].values
iats = np.diff(ts, axis=1)                       # shape: (rows, N-1)
feats['iat_t2_t1']  = ts[:, 1] - ts[:, 0]       # the threshold rule used this
feats['iat_mean']   = np.nanmean(iats, axis=1)
feats['iat_std']    = np.nanstd(iats,  axis=1)
feats['iat_max']    = np.nanmax(iats,  axis=1)

# absolute arrival times of first few packets
feats['t1'] = ts[:, 0]
feats['t2'] = ts[:, 1]
if N >= 3:
    feats['t3'] = df['t3'].values

feats = feats.fillna(0)

X = feats.values
y = df['label'].values

print('Dataset: %d flows  |  mice=%d  elephant=%d' % (len(y), (y==0).sum(), (y==1).sum()))
print('Features:', list(feats.columns))
print()

# ── train / test split ────────────────────────────────────────────────────────
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y)

# ── Random Forest ─────────────────────────────────────────────────────────────
clf = RandomForestClassifier(
    n_estimators=100,
    max_depth=None,
    min_samples_leaf=2,
    random_state=42,
    n_jobs=-1,
    class_weight='balanced'
)
clf.fit(X_train, y_train)
y_pred = clf.predict(X_test)

# ── results ───────────────────────────────────────────────────────────────────
cm = confusion_matrix(y_test, y_pred)   # rows=actual, cols=predicted
TP = cm[1, 1]; FP = cm[0, 1]
FN = cm[1, 0]; TN = cm[0, 0]

precision = precision_score(y_test, y_pred, zero_division=0)
recall    = recall_score(y_test, y_pred,    zero_division=0)
f1        = f1_score(y_test, y_pred,        zero_division=0)

print('=== Confusion Matrix (elephant = positive) ===')
print('               Predicted mice   Predicted elephant')
print('Actual mice       %5d (TN)       %5d (FP)' % (TN, FP))
print('Actual elephant   %5d (FN)       %5d (TP)' % (FN, TP))
print()
print('Precision : %.2f  (%d / (%d+%d))' % (precision, TP, TP, FP))
print('Recall    : %.2f  (%d / (%d+%d))' % (recall,    TP, TP, FN))
print('F1        : %.2f  (2PR / (P+R))'  % f1)
print()

# ── feature importance ────────────────────────────────────────────────────────
print('=== Feature Importance (top 10) ===')
importances = pd.Series(clf.feature_importances_, index=feats.columns)
for name, val in importances.sort_values(ascending=False).head(10).items():
    print('  %-15s %.4f' % (name, val))