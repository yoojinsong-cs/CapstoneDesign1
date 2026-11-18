#!/usr/bin/env bash
# One-time setup for the explicitly authorized Capstone benchmark.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this setup with sudo in your VM terminal.' >&2
    exit 1
fi

benchmark_rule='kth ALL=(root) CWD=/home/kth/CapstoneDesign1/scripts NOPASSWD: /usr/bin/python3 -m scheduler.benchmark --output /home/kth/CapstoneDesign1/scripts/data/comparisons/priority05_legacy_100flows_5pairs'
benchmark_target=/etc/sudoers.d/capstone-benchmark
benchmark_tmpdir=$(mktemp -d /tmp/capstone-sudo-setup.XXXXXX)
trap 'rm -rf -- "$benchmark_tmpdir"' EXIT
printf '%s\n' "$benchmark_rule" > "$benchmark_tmpdir/rule"
/usr/sbin/visudo -c -f "$benchmark_tmpdir/rule"

if [[ -e "$benchmark_target" ]] && ! cmp -s "$benchmark_tmpdir/rule" "$benchmark_target"; then
    echo "A different rule already exists at $benchmark_target; leaving it unchanged." >&2
    exit 1
fi

/usr/bin/apt-get install -y gcc-12
/usr/bin/install -o root -g root -m 0440 "$benchmark_tmpdir/rule" "$benchmark_target"
/usr/sbin/visudo -c -f "$benchmark_target"
echo 'Setup complete. The benchmark command can now run without a sudo password.'
