#!/usr/bin/env bash
# remote write 集成测试：用真实 prometheus 验证手写的 protobuf + snappy 能被接收，
# 并验证 prometheus 不可用期间缓存的数据在恢复后补传成功。
#   源 exporter : 127.0.0.1:19102，python http.server 提供静态 metrics
#   prometheus  : 127.0.0.1:19090，开启 --web.enable-remote-write-receiver
set -euo pipefail

VER="${PROMETHEUS_VERSION:-3.5.0}"
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
  for _ in $(seq 1 100); do
    curl -sf "$1" >/dev/null && return 0
    sleep 0.2
  done
  fail "$1 not up"
}

# 查询 prometheus，打印第一个结果的值
query() {
  curl -sf -G http://127.0.0.1:19090/api/v1/query --data-urlencode "query=$1" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")'
}

# 下载并校验 prometheus
TAR="prometheus-${VER}.linux-amd64.tar.gz"
BASE="https://github.com/prometheus/prometheus/releases/download/v${VER}"
curl -sSfL -o "$WORK/$TAR" "$BASE/$TAR"
curl -sSfL -o "$WORK/sha256sums.txt" "$BASE/sha256sums.txt"
(cd "$WORK" && grep " $TAR\$" sha256sums.txt | sha256sum -c -)
mkdir "$WORK/bin" "$WORK/src" "$WORK/textfile" "$WORK/buffer" "$WORK/conf" "$WORK/data"
tar xzf "$WORK/$TAR" -C "$WORK/bin" --strip-components=1

cat > "$WORK/src/metrics" <<'METRICS'
# HELP it_gauge test gauge
# TYPE it_gauge gauge
it_gauge{a="x \"y\"",b="主机"} 42
it_inf +Inf
METRICS
python3 -m http.server 19102 --bind 127.0.0.1 --directory "$WORK/src" >/dev/null 2>&1 &
PIDS+=($!)
wait_up http://127.0.0.1:19102/metrics

cat > "$WORK/conf/it.ini" <<CONF
[rw]
name : rw
url : http://127.0.0.1:19102/metrics
interval : 1
prometheus_url : http://127.0.0.1:19090/api/v1/write
CONF
printf 'global:\n  scrape_interval: 1h\n' > "$WORK/prometheus.yml"

status() {
  grep -E "^prom_textfile_remote_write_$1\{" "$WORK/textfile/it_rw_remote_write.prom" | awk '{print $NF}'
}

echo "== prometheus down, buffer"
# 非交互 bash 的后台进程会继承 SIGINT=ignore，开启 job control 才能用 kill -INT 正常停止
set -m
python3 "$ROOT/prom-textfile.py" -c "$WORK/conf" -p "$WORK/textfile" -s "$WORK/state" -b "$WORK/buffer" --daemon >/dev/null 2>&1 &
DPID=$!
PIDS+=($DPID)
set +m
sleep 4
[ "$(status up)" = "0" ] || fail "remote_write_up should be 0 while prometheus is down"
buffered=$(status buffer_files)
[ "$buffered" -ge 3 ] || fail "expected >= 3 buffered files, got $buffered"
[ -f "$WORK/textfile/it_rw.prom" ] && fail "job with prometheus_url must not write textfile"
echo "ok: $buffered files buffered"

echo "== prometheus up, backfill"
"$WORK/bin/prometheus" --config.file="$WORK/prometheus.yml" --storage.tsdb.path="$WORK/data" \
  --web.listen-address=127.0.0.1:19090 --web.enable-remote-write-receiver >"$WORK/prometheus.log" 2>&1 &
PIDS+=($!)
wait_up http://127.0.0.1:19090/-/ready
sleep 4
[ "$(status up)" = "1" ] || fail "remote_write_up should be 1 after prometheus is up"
[ "$(status buffer_files)" = "0" ] || fail "buffer not drained: $(status buffer_files)"
[ "$(status rejected_files)" = "0" ] || fail "rejected files: $(status rejected_files)"

# job_count 从 1 开始递增，最小值为 1 说明 prometheus 启动前缓存的第一批也补传上去了
[ "$(query 'min_over_time(prom_textfile_job_count{prom_cronjob_name="rw"}[10m])')" = "1" ] \
  || fail "first buffered batch missing"
total=$(query 'count_over_time(prom_textfile_job_count{prom_cronjob_name="rw"}[10m])')
[ "$total" -ge 6 ] || fail "expected >= 6 samples, got $total"
[ "$(query 'it_gauge{a="x \"y\"",b="主机",prom_cronjob_name="rw"}')" = "42" ] || fail "it_gauge missing"
[ "$(query 'it_inf{prom_cronjob_name="rw"}')" = "+Inf" ] || fail "it_inf missing"
[ "$(query 'prom_textfile_job_up{prom_cronjob_name="rw"}')" = "1" ] || fail "job_up missing"
echo "ok: $total samples in prometheus, including backfill"

kill -INT "$DPID"
for _ in $(seq 1 50); do
  kill -0 "$DPID" 2>/dev/null || break
  sleep 0.2
done
kill -0 "$DPID" 2>/dev/null && fail "daemon did not stop on SIGINT"
rc=0
wait "$DPID" || rc=$?
[ "$rc" -eq 0 ] || fail "daemon exit code $rc"

echo "remote write integration OK"
