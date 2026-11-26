#!/usr/bin/env bash
# remote write 集成测试：用真实 prometheus 验证手写的 protobuf + snappy 能被接收，
# 验证 prometheus 不可用期间缓存的数据在恢复后补传成功，以及无认证 / basic auth / bearer token 三种认证。
#   源 exporter   : 127.0.0.1:19102，python http.server 提供静态 metrics
#   prometheus    : 127.0.0.1:19090，开启 --web.enable-remote-write-receiver，无认证
#   prometheus    : 127.0.0.1:19091，--web.config.file 开启 basic auth
#   bearer 代理   : 127.0.0.1:19092，校验 token 后转发到 19090（prometheus 本身不支持 bearer token）
# 需要 htpasswd（apache2-utils / httpd-tools）生成 basic auth 的 bcrypt 密码
set -euo pipefail

command -v htpasswd >/dev/null || { echo "FAIL: htpasswd not found, install apache2-utils" >&2; exit 1; }

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

# wait_up <url> [curl 参数...]
wait_up() {
  local url="$1"
  shift
  for _ in $(seq 1 100); do
    curl -sf "$@" "$url" >/dev/null && return 0
    sleep 0.2
  done
  fail "$url not up"
}

# 查询 prometheus，打印第一个结果的值: query_at <端口> <promql> [curl 参数...]
query_at() {
  local port="$1" expr="$2"
  shift 2
  curl -sf "$@" -G "http://127.0.0.1:$port/api/v1/query" --data-urlencode "query=$expr" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")'
}

query() { query_at 19090 "$1"; }

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
[DEFAULT]
labels : env=it,a=custom
labels_overwrite : b=over

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
[ "$(query 'it_gauge{a="custom",exported_a="x \"y\"",b="over",env="it",prom_cronjob_name="rw"}')" = "42" ] \
  || fail "it_gauge with custom labels missing"
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

echo "== auth"
BASIC_USER=rwuser
BASIC_PASS='p@ss:word'
TOKEN=secret-token
printf 'basic_auth_users:\n  %s: %s\n' "$BASIC_USER" "$(htpasswd -nbBC 10 "" "$BASIC_PASS" | tr -d ':\n')" > "$WORK/web.yml"
mkdir "$WORK/data-basic"
"$WORK/bin/prometheus" --config.file="$WORK/prometheus.yml" --storage.tsdb.path="$WORK/data-basic" \
  --web.listen-address=127.0.0.1:19091 --web.enable-remote-write-receiver \
  --web.config.file="$WORK/web.yml" >"$WORK/prometheus-basic.log" 2>&1 &
PIDS+=($!)
python3 "$ROOT/tests/bearer_proxy.py" 19092 http://127.0.0.1:19090 "$TOKEN" &
PIDS+=($!)
wait_up http://127.0.0.1:19091/-/ready -u "$BASIC_USER:$BASIC_PASS"
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:19091/-/ready)" = "401" ] \
  || fail "basic auth prometheus accepts requests without auth"

# auth_case <名称> <remote write 地址> <错误的认证配置> <正确的认证配置> <查询端口> [查询用的 curl 参数...]
# cron 模式各跑一次：错误凭据应 401、数据留在缓存；正确凭据应补传，两批数据都写进 prometheus
auth_case() {
  local name="$1" url="$2" bad="$3" good="$4" port="$5"
  shift 5
  local dir="$WORK/auth-$name"
  mkdir -p "$dir/conf" "$dir/textfile"
  local st="$dir/textfile/auth_${name}_remote_write.prom"
  conf() {
    printf '[%s]\nname : %s\nurl : http://127.0.0.1:19102/metrics\ninterval : 30\nprometheus_url : %s\n%b' \
      "$name" "$name" "$url" "$1" > "$dir/conf/auth.ini"
  }
  run_once() {
    python3 "$ROOT/prom-textfile.py" -c "$dir/conf/auth.ini" -p "$dir/textfile" -s "$dir/state" -b "$dir/buffer" \
      > "$dir/log" 2>&1 || { cat "$dir/log" >&2; fail "$name: prom-textfile failed"; }
  }
  value() { grep -E "^prom_textfile_remote_write_$1\{" "$st" | awk '{print $NF}'; }

  conf "$bad"
  run_once
  grep -q 'remote write .* 401' "$dir/log" || fail "$name: expected 401 with bad credentials"
  [ "$(value up)" = "0" ] && [ "$(value buffer_files)" = "1" ] && [ "$(value rejected_files)" = "0" ] \
    || fail "$name: bad credentials should keep 1 buffered file"

  conf "$good"
  sleep 1
  run_once
  [ "$(value up)" = "1" ] && [ "$(value buffer_files)" = "0" ] && [ "$(value rejected_files)" = "0" ] \
    || fail "$name: good credentials should drain the buffer"
  local n
  n=$(query_at "$port" "count_over_time(prom_textfile_job_count{prom_cronjob_name=\"$name\"}[10m])" "$@")
  [ "$n" = "2" ] || fail "$name: expected 2 samples in prometheus, got $n"
  [ "$(query_at "$port" "it_gauge{prom_cronjob_name=\"$name\"}" "$@")" = "42" ] || fail "$name: it_gauge missing"
  echo "ok: $name, 401 buffered then backfilled"
}

auth_case basic http://127.0.0.1:19091/api/v1/write \
  "prometheus_username : $BASIC_USER\nprometheus_password : wrong\n" \
  "prometheus_username : $BASIC_USER\nprometheus_password : $BASIC_PASS\n" \
  19091 -u "$BASIC_USER:$BASIC_PASS"
auth_case noauth_to_basic http://127.0.0.1:19091/api/v1/write \
  "" \
  "prometheus_username : $BASIC_USER\nprometheus_password : $BASIC_PASS\n" \
  19091 -u "$BASIC_USER:$BASIC_PASS"
auth_case bearer http://127.0.0.1:19092/api/v1/write \
  "prometheus_bearer_token : wrong\n" \
  "prometheus_bearer_token : $TOKEN\n" \
  19090
auth_case bearer_over_basic http://127.0.0.1:19092/api/v1/write \
  "prometheus_username : $BASIC_USER\nprometheus_password : $BASIC_PASS\n" \
  "prometheus_username : $BASIC_USER\nprometheus_password : $BASIC_PASS\nprometheus_bearer_token : $TOKEN\n" \
  19090

echo "remote write integration OK"
