#!/usr/bin/env bash
# 集成测试：用真实 node_exporter 验证 prom-textfile 产出的 .prom 能被 textfile collector 正确读取。
#   源 exporter : 127.0.0.1:19101，关闭 textfile collector，作为被拉取的目标
#   宿 exporter : 127.0.0.1:19100，开启 textfile collector，读取 prom-textfile 的输出
set -euo pipefail

VER="${NODE_EXPORTER_VERSION:-1.9.1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

wait_up() {
  for _ in $(seq 1 50); do
    curl -sf "$1" >/dev/null && return 0
    sleep 0.2
  done
  fail "$1 not up"
}

# 下载并校验 node_exporter
TAR="node_exporter-${VER}.linux-amd64.tar.gz"
BASE="https://github.com/prometheus/node_exporter/releases/download/v${VER}"
curl -sSfL -o "$WORK/$TAR" "$BASE/$TAR"
curl -sSfL -o "$WORK/sha256sums.txt" "$BASE/sha256sums.txt"
(cd "$WORK" && grep " $TAR\$" sha256sums.txt | sha256sum -c -)
mkdir "$WORK/bin" "$WORK/textfile" "$WORK/conf"
tar xzf "$WORK/$TAR" -C "$WORK/bin" --strip-components=1
NE="$WORK/bin/node_exporter"

"$NE" --web.listen-address=127.0.0.1:19101 --no-collector.textfile >/dev/null 2>&1 &
PIDS+=($!)
"$NE" --web.listen-address=127.0.0.1:19100 --collector.textfile.directory="$WORK/textfile" >/dev/null 2>&1 &
PIDS+=($!)
wait_up http://127.0.0.1:19101/metrics
wait_up http://127.0.0.1:19100/metrics

# 自定义 label：labels 里的 mode 与 node_cpu_seconds_total 自带的 mode 冲突，原值应改名为 exported_mode
# 这里不用 labels_overwrite 覆盖 mode：多个 cpu mode 会变成重复 series，node_exporter 会报错
cat > "$WORK/conf/it.ini" <<CONF
[DEFAULT]
labels : env=it,team="a,b"

[node]
name : node
url : http://127.0.0.1:19101/metrics
interval : 1
labels : mode=custom

[down]
name : down
url : http://127.0.0.1:1/metrics
interval : 1
CONF

check_scrape() {
  curl -sf http://127.0.0.1:19100/metrics > "$WORK/scrape.txt"
  grep -q '^node_textfile_scrape_error 0$' "$WORK/scrape.txt" \
    || fail "textfile scrape error: $(grep node_textfile_scrape_error "$WORK/scrape.txt")"
  grep -Eq '^prom_textfile_job_up\{[^}]*prom_cronjob_name="node"[^}]*\} 1$' "$WORK/scrape.txt" \
    || fail "job_up for node != 1"
  grep -Eq '^prom_textfile_job_up\{[^}]*prom_cronjob_name="down"[^}]*\} 0$' "$WORK/scrape.txt" \
    || fail "job_up for down != 0"
  n=$(grep -c 'prom_cronjob_name="node"' "$WORK/scrape.txt" || true)
  [ "$n" -gt 100 ] || fail "only $n relabeled node metrics"
  grep -Eq '^node_cpu_seconds_total\{[^}]*env="it",exported_mode="idle",mode="custom"[^}]*team="a,b"' "$WORK/scrape.txt" \
    || fail "custom labels / exported_mode missing on node_cpu_seconds_total"
  grep -Eq '^prom_textfile_job_up\{[^}]*env="it"[^}]*prom_cronjob_name="down"' "$WORK/scrape.txt" \
    || fail "custom labels missing on job_up"
  echo "ok: $n relabeled node metrics"
}

echo "== cron mode"
python3 "$ROOT/prom-textfile.py" -c "$WORK/conf" -p "$WORK/textfile" -s "$WORK/state"
if ls "$WORK/textfile" | grep -q '\.tmp$'; then fail "tmp file left"; fi
check_scrape

echo "== daemon mode"
rm -f "$WORK"/textfile/*
# 非交互 bash 的后台进程会继承 SIGINT=ignore，开启 job control 才能用 kill -INT 正常停止
set -m
python3 "$ROOT/prom-textfile.py" -c "$WORK/conf" -p "$WORK/textfile" -s "$WORK/state" --daemon >/dev/null 2>&1 &
DPID=$!
PIDS+=($DPID)
set +m
sleep 4
check_scrape
count=$(grep -E '^prom_textfile_job_count\{[^}]*prom_cronjob_name="node"' "$WORK/scrape.txt" | awk '{print $NF}')
[ "$count" -ge 2 ] || fail "daemon job_count=$count, expected >= 2"
kill -INT "$DPID"
for _ in $(seq 1 50); do
  kill -0 "$DPID" 2>/dev/null || break
  sleep 0.2
done
kill -0 "$DPID" 2>/dev/null && fail "daemon did not stop on SIGINT"
rc=0
wait "$DPID" || rc=$?
[ "$rc" -eq 0 ] || fail "daemon exit code $rc"

echo "integration OK"
