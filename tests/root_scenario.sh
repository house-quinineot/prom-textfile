#!/usr/bin/env bash
# root 场景测试：按 readme 的方式安装后用 sudo 运行，-c 指向单个配置文件，
# 并且 /tmp 下已有普通用户创建的旧计数文件（fs.protected_regular 下 root 写不了它）。
# 需要普通用户 + 免密 sudo，会写 /var/lib/prom-textfile，只在 CI 里跑。
#   用法: tests/root_scenario.sh [要测试的 prom-textfile.py，默认当前仓库的]
set -euo pipefail

SCRIPT="${1:-$(cd "$(dirname "$0")/.." && pwd)/prom-textfile.py}"
OLD=/tmp/prom-text-self.txt
STATE=/var/lib/prom-textfile

fail() { echo "FAIL: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || fail "run as a regular user with passwordless sudo"
sudo -n true || fail "passwordless sudo required"
[ -e "$OLD" ] && fail "$OLD already exists, refusing to touch it"
[ -e "$STATE" ] && fail "$STATE already exists, refusing to touch it"
# 很多发行版默认 fs.protected_regular=2（GitHub runner 是 0），设成 2 才能复现 root 写不了别人在 /tmp 的文件
PROTECTED="$(cat /proc/sys/fs/protected_regular)"
sudo sysctl -qw fs.protected_regular=2
echo "fs.protected_regular=$(cat /proc/sys/fs/protected_regular) (was $PROTECTED)"

WORK="$(mktemp -d)"
PIDS=()
cleanup() {
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  sudo rm -rf "$WORK" "$OLD" "$STATE"
  sudo sysctl -qw fs.protected_regular="$PROTECTED"
}
trap cleanup EXIT

mkdir "$WORK/src" "$WORK/etc" "$WORK/bin" "$WORK/out"
printf '# TYPE m gauge\nm 1\n' > "$WORK/src/metrics"
python3 -m http.server 19103 --bind 127.0.0.1 --directory "$WORK/src" >/dev/null 2>&1 &
PIDS+=($!)
for _ in $(seq 1 50); do curl -sf http://127.0.0.1:19103/metrics >/dev/null && break; sleep 0.2; done
printf '[node]\nname : node\nurl : http://127.0.0.1:19103/metrics\ninterval : 30\n' > "$WORK/etc/mac-mini.ini"

# 按 readme 安装：可执行文件 + shebang，sudo 下走系统 python3
sudo install -m 0755 "$SCRIPT" "$WORK/bin/prom-textfile"

# 普通用户留下的旧计数文件
echo 1 > "$OLD"

run() {
  local rc=0
  sudo "$WORK/bin/prom-textfile" "$@" -p "$WORK/out" --debug > "$WORK/log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ] || grep -q 'Traceback' "$WORK/log"; then
    cat "$WORK/log" >&2
    fail "prom-textfile $* exit code $rc"
  fi
}

job_count() {
  grep -E '^prom_textfile_job_count\{' "$WORK/out/mac-mini_node.prom" | awk '{print $NF}'
}

echo "== sudo, -c single file"
run -c "$WORK/etc/mac-mini.ini"
grep -q "start config:\[\]" "$WORK/log" && fail "no job loaded from config file"
grep -Eq '^prom_textfile_job_up\{[^}]*\} 1$' "$WORK/out/mac-mini_node.prom" || fail "job_up != 1"
[ "$(job_count)" = "1" ] || fail "job_count=$(job_count), expected 1"

echo "== sudo, -c directory, counter keeps going"
run -c "$WORK/etc"
[ "$(job_count)" = "2" ] || fail "job_count=$(job_count), expected 2"

[ "$(stat -c %U "$OLD")" = "$(id -un)" ] && [ "$(cat "$OLD")" = "1" ] || fail "$OLD was modified"
sudo test -f "$STATE/state/prom-textfile-self.txt" || fail "self counter not in $STATE/state"
sudo test -f "$STATE/state/mac-mini_node.txt" || fail "job counter not in $STATE/state"

echo "root scenario OK"
