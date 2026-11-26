[toc]

# prom textfile

获取本机里所有exporter 监控数据写入到本地的node exporter textfile目录下，prometheus获取一次就可以拿到所有的机器的全部监控。

本脚本使用python3默认模块，无需安装python其他模块。

可以使用supervisor或systemd来启动或管理进程。也可以定时任务

*使用uspervisor记得开启node exporter 监控supervisor参数。*

配置文件支持多文件，文件内多配置

# run

```sh
python3 prom-textfile.py -c /etc/prom-textfile -p /var/lib/prometheus/node-exporter --debug
```

# test

单元测试，仅依赖 python3 标准库：

```sh
python3 -m unittest discover -s tests -v
```

集成测试，会下载 node_exporter（校验 sha256）并启动两个实例，验证 cron/daemon 两种模式的输出能被 textfile collector 正常读取（`node_textfile_scrape_error 0`）。需要 linux amd64、bash、curl，占用端口 19100/19101：

```sh
tests/integration.sh
# 指定 node_exporter 版本，默认 1.9.1
NODE_EXPORTER_VERSION=1.9.1 tests/integration.sh
```

# CI

GitHub Actions 配置在 `.github/workflows/`：

| workflow | 触发 | 内容 |
|---|---|---|
| `test.yml` | push 到非 main 分支、所有 pull request | Python 3.12 语法检查 + 单元测试 |
| `full.yml` | push 到 main、手动触发（workflow_dispatch） | Python 3.9–3.14 × ubuntu-22.04/24.04 单元测试矩阵，全部通过后跑集成测试 |

来自 fork 的 pull request，首次运行 workflow 需要仓库维护者在 PR 页面批准。

# add default job label

`{prom_cronjob_interval="interval",prom_cronjob_name="job name",prom_cronjob_url="url"}`

# 增加监控项

prom_textfile_job_up

prom_textfile_job_count
# install

## install prom-textfile

```sh
sudo mkdir -p /etc/prom-textfile
sudo curl -L 'https://raw.githubusercontent.com/yaule/prom-textfile/main/prom-textfile.py' -o /usr/bin/prom-textfile
sudo chmod +x /usr/bin/prom-textfile
```

## systemd config scripts

### install

```sh
sudo tee /usr/lib/systemd/system/prom-textfile.service<<-EOF
[Unit]
Description=prom-textfile
Documentation=man:prom-textfile
Documentation=https://github.com/yaule/prom-textfile
[Service]
ExecStart=/usr/bin/prom-textfile --daemon
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl restart prom-textfile
sudo systemctl enable prom-textfile
```

### remove

```sh
sudo systemctl stop prom-textfile
sudo systemctl disable prom-textfile
sudo rm -rf /etc/prom-textfile /usr/bin/prom-textfile /usr/lib/systemd/system/prom-textfile.service
sudo systemctl daemon-reload
```

## supervisor
### install

```sh
sudo mkdir -p /etc/prom-textfile
sudo apt install supervisor -y
# supervisord config
sudo tee /etc/supervisor/supervisord.conf<<-EOF
[unix_http_server]
file=/var/run/supervisor.sock   ; (the path to the socket file)
chmod=0700                       ; sockef file mode (default 0700)
[inet_http_server]
port=localhost:9001
[supervisord]
logfile=/var/log/supervisor/supervisord.log ; (main log file;default $CWD/supervisord.log)
pidfile=/var/run/supervisord.pid ; (supervisord pidfile;default supervisord.pid)
childlogdir=/var/log/supervisor            ; ('AUTO' child log dir, default $TEMP)
[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix:///var/run/supervisor.sock ; use a unix:// URL  for a unix socket
[include]
files = /etc/supervisor/conf.d/*.conf
EOF

sudo systemctl start supervisor
sudo systemctl enable supervisor

# supervisor prom-textfile config
sudo tee /etc/supervisor/conf.d/prom-textfile.conf <<-EOF
[program:prom-textfile]
command=/usr/bin/prom-textfile --daemon
process_name=%(program_name)s
numprocs=1
minfds = 10000
minprocs = 10000
umask=022
autostart=true
startsecs=10
startretries=3
exitcodes=0
stopsignal=TERM
stopwaitsecs=10
stopasgroup=false
killasgroup=false
user=root
EOF
sudo supervisorctl reload
# enable prometheus node exporter supervisor
sudo tee /etc/default/prometheus-node-exporter <<-EOF
ARGS="--collector.supervisord"
EOF
sudo systemctl restart prometheus-node-exporter
```
### remove

```sh
sudo rm -rf /etc/prom-textfile
sudo rm -f /etc/supervisor/conf.d/prom-textfile.conf
sudo supervisorctl reload
# remove supervisor
sudo apt remove supervisor -y
```

## cron job

### install

```sh
sudo tee /usr/lib/systemd/system/prom-textfile.timer <<-EOF
[Unit]
Description=Run prom-textfile every 1 minute
[Timer]
OnBootSec=0
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF
sudo tee /usr/lib/systemd/system/prom-textfile.service <<-EOF
[Unit]
Description=Collect textfile metrics for prometheus-node-exporter
[Service]
Type=oneshot
ExecStart=/bin/bash -c "/usr/bin/prom-textfile"
EOF
sudo systemctl daemon-reload
sudo systemctl enable prom-textfile.timer
sudo systemctl start prom-textfile.service prom-textfile.timer
```

### remove

```sh
sudo systemctl disable prom-textfile.timer prom-textfile.service
sudo systemctl stop prom-textfile.service prom-textfile.timer
sudo rm -f /usr/lib/systemd/system/prom-textfile.timer
sudo rm -f /usr/lib/systemd/system/prom-textfile.service
sudo systemctl daemon-reload
```

