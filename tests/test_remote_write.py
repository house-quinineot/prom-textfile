#!/usr/bin/env python3
"""
remote write 测试，只用标准库。测试侧自带 snappy / protobuf 解码，用来校验发出去的请求。

运行: python3 -m unittest discover -s tests -v
"""

import asyncio
import base64
import fcntl
import http.server
import importlib.util
import logging
import math
import os
import struct
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'prom-textfile.py')

# 文件名带 "-"，不能直接 import
_spec = importlib.util.spec_from_file_location('prom_textfile_rw', SCRIPT)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


def read_varint(data, i):
    n = shift = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7f) << shift
        shift += 7
        if b < 0x80:
            return n, i


def snappy_decode(data):
    """只支持 literal，prom-textfile 只会生成 literal"""
    length, i = read_varint(data, 0)
    out = bytearray()
    while i < len(data):
        tag = data[i]
        i += 1
        assert tag & 3 == 0, 'unexpected snappy copy tag'
        n = tag >> 2
        if n >= 60:
            extra = n - 59
            n = int.from_bytes(data[i:i + extra], 'little')
            i += extra
        n += 1
        out += data[i:i + n]
        i += n
    assert len(out) == length
    return bytes(out)


def pb_fields(data):
    i = 0
    while i < len(data):
        key, i = read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = read_varint(data, i)
        elif wire == 1:
            value = data[i:i + 8]
            i += 8
        elif wire == 2:
            n, i = read_varint(data, i)
            value = data[i:i + n]
            i += n
        else:
            raise AssertionError('unexpected wire type {}'.format(wire))
        yield field, wire, value


def decode_write_request(body):
    """返回 [(labels 列表(保持顺序), value, timestamp)]"""
    series = []
    for field, _, ts in pb_fields(body):
        assert field == 1
        labels, value, timestamp = [], None, None
        for f, _, v in pb_fields(ts):
            if f == 1:
                kv = dict((lf, lv.decode()) for lf, _, lv in pb_fields(v))
                labels.append((kv[1], kv[2]))
            elif f == 2:
                for sf, _, sv in pb_fields(v):
                    if sf == 1:
                        value = struct.unpack('<d', sv)[0]
                    elif sf == 2:
                        timestamp = sv
        series.append((labels, value, timestamp))
    return series


class _RemoteWriteHandler(http.server.BaseHTTPRequestHandler):
    """
    记录收到的 remote write 请求，按 codes 队列返回状态码，队列空了返回 204。
    auth 不为 None 时模拟需要认证的 prometheus：Authorization 不等于 auth 返回 401，不记录数据
    """

    requests = []
    codes = []
    auth = None
    unauthorized = 0

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        cls = type(self)
        if cls.auth is not None and self.headers.get('Authorization') != cls.auth:
            cls.unauthorized += 1
            self.send_response(401)
            self.end_headers()
            return
        cls.requests.append((dict(self.headers), body))
        code = cls.codes.pop(0) if cls.codes else 204
        self.send_response(code)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'# HELP m test\n# TYPE m gauge\nm{a="x \\"y\\""} 1\nup 1\n')

    def log_message(self, *args):
        pass


class RemoteWriteServerMixin:

    @classmethod
    def setUpClass(cls):
        http.server.ThreadingHTTPServer.allow_reuse_address = True
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _RemoteWriteHandler)
        cls.base_url = 'http://127.0.0.1:{}'.format(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        logging.disable(logging.CRITICAL)
        _RemoteWriteHandler.requests = []
        _RemoteWriteHandler.codes = []
        _RemoteWriteHandler.auth = None
        _RemoteWriteHandler.unauthorized = 0
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def sent(self):
        """解码所有收到的请求"""
        return [decode_write_request(snappy_decode(body)) for _, body in _RemoteWriteHandler.requests]


class TestEncoding(unittest.TestCase):

    def test_snappy_roundtrip(self):
        for n in (0, 1, 59, 60, 255, 256, 65536, 65537, 200000):
            with self.subTest(n=n):
                data = os.urandom(n)
                self.assertEqual(snappy_decode(pt.snappy_compress(data)), data)

    def test_encode_labels_sorted_with_name(self):
        body = pt.encode_write_request([({'__name__': 'm', 'b': '2', 'a': '1'}, 1.5)], 1700000000123)
        self.assertEqual(decode_write_request(body),
                         [([('__name__', 'm'), ('a', '1'), ('b', '2')], 1.5, 1700000000123)])

    def test_encode_special_values(self):
        body = pt.encode_write_request(
            [({'__name__': 'a'}, float('inf')), ({'__name__': 'b'}, float('-inf')),
             ({'__name__': 'c'}, float('nan')), ({'__name__': 'd'}, -2.0)], 1)
        values = [v for _, v, _ in decode_write_request(body)]
        self.assertEqual(values[0], float('inf'))
        self.assertEqual(values[1], float('-inf'))
        self.assertTrue(math.isnan(values[2]))
        self.assertEqual(values[3], -2.0)

    def test_encode_utf8_label(self):
        body = pt.encode_write_request([({'__name__': 'm', 'host': '主机'}, 1.0)], 1)
        self.assertEqual(decode_write_request(body)[0][0], [('__name__', 'm'), ('host', '主机')])

    def test_parse_samples(self):
        text = ('# HELP m test\n# TYPE m gauge\n'
                'm{a="x \\"y\\"",b="c\\\\d",n="l1\\nl2"} 2\n'
                'ns:rule 1e3\nh_bucket{le="+Inf"} +Inf\n\n')
        samples = pt.parse_samples(text)
        self.assertEqual(samples, [
            ({'a': 'x "y"', 'b': 'c\\d', 'n': 'l1\nl2', '__name__': 'm'}, 2.0),
            ({'__name__': 'ns:rule'}, 1000.0),
            ({'le': '+Inf', '__name__': 'h_bucket'}, float('inf')),
        ])

    def test_parse_samples_skips_bad_and_dedupes(self):
        logging.disable(logging.CRITICAL)
        try:
            samples = pt.parse_samples('{broken\nm abc\nm{a="1"} 1\nm{a="1"} 2\n')
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(samples, [({'a': '1', '__name__': 'm'}, 2.0)])


class TestFlush(RemoteWriteServerMixin, unittest.TestCase):

    def make(self, **conf):
        conf.setdefault('prometheus_url', self.base_url + '/api/v1/write')
        conf.setdefault('prom_file_name', 'x_job')
        return pt.remote_write(conf, self.tmp.name)

    def save(self, rw, ages_s):
        """按「多少秒以前」写入缓存文件，返回时间戳列表"""
        now = int(time.time() * 1000)
        ts = [now - a * 1000 for a in ages_s]
        for t in ts:
            rw.save(['m{{t="{}"}} 1\n'.format(t)], t)
        return ts

    def buffered(self, rw, sub=''):
        return sorted(f for f in os.listdir(os.path.join(rw.buffer_dir, sub)) if f.endswith('.prom'))

    def test_success_oldest_first_and_deleted(self):
        rw = self.make()
        ts = self.save(rw, [10, 30, 20])
        self.assertEqual(rw.flush(), (1, 0, 0))
        self.assertEqual([s[0][2] for s in self.sent()], sorted(ts))
        self.assertEqual(self.buffered(rw), [])

    def test_headers(self):
        rw = self.make()
        self.save(rw, [1])
        rw.flush()
        headers = _RemoteWriteHandler.requests[0][0]
        self.assertEqual(headers['Content-Encoding'], 'snappy')
        self.assertEqual(headers['Content-Type'], 'application/x-protobuf')
        self.assertEqual(headers['X-Prometheus-Remote-Write-Version'], '0.1.0')
        self.assertNotIn('Authorization', headers)

    def test_basic_auth(self):
        rw = self.make(prometheus_username='u', prometheus_password='p:w')
        self.save(rw, [1])
        rw.flush()
        self.assertEqual(_RemoteWriteHandler.requests[0][0]['Authorization'],
                         'Basic ' + base64.b64encode(b'u:p:w').decode())

    def test_bearer_token_wins_over_basic(self):
        rw = self.make(prometheus_username='u', prometheus_password='p', prometheus_bearer_token='tok')
        self.save(rw, [1])
        rw.flush()
        self.assertEqual(_RemoteWriteHandler.requests[0][0]['Authorization'], 'Bearer tok')

    def test_server_error_keeps_all_and_stops(self):
        rw = self.make()
        self.save(rw, [30, 20, 10])
        for code in (500, 503, 429, 401):
            with self.subTest(code=code):
                _RemoteWriteHandler.requests = []
                _RemoteWriteHandler.codes = [code]
                self.assertEqual(rw.flush(), (0, 3, 0))
                self.assertEqual(len(_RemoteWriteHandler.requests), 1)

    def test_network_down_keeps_all(self):
        rw = self.make(prometheus_url='http://127.0.0.1:1/api/v1/write')
        self.save(rw, [20, 10])
        self.assertEqual(rw.flush(), (0, 2, 0))

    def test_bad_request_moved_to_rejected_and_continue(self):
        rw = self.make()
        ts = self.save(rw, [30, 20, 10])
        _RemoteWriteHandler.codes = [204, 400]
        self.assertEqual(rw.flush(), (1, 0, 1))
        self.assertEqual(len(_RemoteWriteHandler.requests), 3)
        self.assertEqual(self.buffered(rw, 'rejected'), ['{}.prom'.format(sorted(ts)[1])])

    def test_backfill_after_recovery(self):
        rw = self.make()
        self.save(rw, [30])
        _RemoteWriteHandler.codes = [503]
        self.assertEqual(rw.flush(), (0, 1, 0))
        self.save(rw, [20, 10])
        self.assertEqual(rw.flush(), (1, 0, 0))
        stamps = [s[0][2] for s in self.sent()]
        # 第一次失败的那个先补传，之后按时间顺序
        self.assertEqual(stamps[1:], sorted(stamps[1:]))
        self.assertEqual(stamps[0], stamps[1])

    def test_retention_max_age(self):
        rw = self.make()
        self.save(rw, [25 * 3600, 10])
        self.assertEqual(rw.flush(), (1, 0, 0))
        self.assertEqual(len(_RemoteWriteHandler.requests), 1)

    def test_retention_max_size_drops_oldest(self):
        rw = self.make()
        ts = self.save(rw, [40, 30, 20, 10])
        size = os.path.getsize(os.path.join(rw.buffer_dir, '{}.prom'.format(ts[0])))
        with mock.patch.object(pt, 'BUFFER_MAX_SIZE', size * 2):
            self.assertEqual(rw.flush(), (1, 0, 0))
        self.assertEqual([s[0][2] for s in self.sent()], ts[2:])

    def test_retention_applies_to_rejected(self):
        rw = self.make()
        os.makedirs(rw.rejected_dir)
        old = int(time.time() * 1000) - 25 * 3600 * 1000
        with open(os.path.join(rw.rejected_dir, '{}.prom'.format(old)), 'w') as f:
            f.write('m 1\n')
        self.save(rw, [1])
        self.assertEqual(rw.flush(), (1, 0, 0))

    def test_locked_by_other_process(self):
        rw = self.make()
        self.save(rw, [1])
        with open(os.path.join(rw.buffer_dir, '.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(rw.flush())
        self.assertEqual(_RemoteWriteHandler.requests, [])
        self.assertEqual(len(self.buffered(rw)), 1)


def basic(user, password):
    return 'Basic ' + base64.b64encode('{}:{}'.format(user, password).encode()).decode()


class TestAuth(RemoteWriteServerMixin, unittest.TestCase):
    """模拟需要认证的 prometheus：无认证、basic auth、bearer token，正确和错误凭据"""

    def make(self, **conf):
        conf.setdefault('prometheus_url', self.base_url + '/api/v1/write')
        conf.setdefault('prom_file_name', 'x_job')
        return pt.remote_write(conf, self.tmp.name)

    def save(self, rw, value):
        rw.save(['m {}\n'.format(value)], int(time.time() * 1000) - (10 - value) * 1000)

    def received_values(self):
        return [series[0][1] for series in self.sent()]

    def check(self, server_auth, good, bad):
        """bad 凭据：401，数据留在缓存；换成 good 凭据：补传，数据按顺序到达"""
        _RemoteWriteHandler.auth = server_auth
        rw = self.make(**bad)
        self.save(rw, 1)
        self.assertEqual(rw.flush(), (0, 1, 0))
        self.assertEqual(_RemoteWriteHandler.unauthorized, 1)
        self.assertEqual(_RemoteWriteHandler.requests, [])

        rw = self.make(**good)
        self.save(rw, 2)
        self.assertEqual(rw.flush(), (1, 0, 0))
        self.assertEqual(self.received_values(), [1.0, 2.0])
        for headers, _ in _RemoteWriteHandler.requests:
            self.assertEqual(headers.get('Authorization'), server_auth)

    def test_no_auth(self):
        rw = self.make()
        self.save(rw, 1)
        self.assertEqual(rw.flush(), (1, 0, 0))
        self.assertEqual(self.received_values(), [1.0])
        self.assertNotIn('Authorization', _RemoteWriteHandler.requests[0][0])

    def test_no_auth_client_to_auth_server(self):
        self.check(basic('u', 'p'), good={'prometheus_username': 'u', 'prometheus_password': 'p'}, bad={})

    def test_basic_auth(self):
        self.check(basic('u', 'p:w'),
                   good={'prometheus_username': 'u', 'prometheus_password': 'p:w'},
                   bad={'prometheus_username': 'u', 'prometheus_password': 'wrong'})

    def test_basic_auth_empty_password(self):
        self.check(basic('u', ''), good={'prometheus_username': 'u'},
                   bad={'prometheus_username': 'other'})

    def test_bearer_token(self):
        self.check('Bearer tok', good={'prometheus_bearer_token': 'tok'},
                   bad={'prometheus_bearer_token': 'wrong'})

    def test_bearer_token_preferred_over_basic(self):
        self.check('Bearer tok',
                   good={'prometheus_bearer_token': 'tok', 'prometheus_username': 'u', 'prometheus_password': 'p'},
                   bad={'prometheus_username': 'u', 'prometheus_password': 'p'})

    def test_config_file_auth(self):
        """从 ini 读取认证配置，cron 模式跑 main()"""
        for server_auth, conf in (
                (None, ''),
                (basic('u', 'p:w'), 'prometheus_username : u\nprometheus_password : p:w\n'),
                ('Bearer tok', 'prometheus_bearer_token : tok\n')):
            with self.subTest(server_auth=server_auth), tempfile.TemporaryDirectory() as d:
                _RemoteWriteHandler.auth = server_auth
                _RemoteWriteHandler.requests = []
                with open(os.path.join(d, 'a.ini'), 'w') as f:
                    f.write('[rw]\nname : rw\nurl : {0}/metrics\ninterval : 30\n'
                            'prometheus_url : {0}/api/v1/write\n{1}'.format(self.base_url, conf))
                asyncio.run(pt.main(os.path.join(d, '*.ini'), d, False,
                                    os.path.join(d, 'buffer'), os.path.join(d, 'state')))
                self.assertEqual(len(_RemoteWriteHandler.requests), 1)
                self.assertEqual(_RemoteWriteHandler.requests[0][0].get('Authorization'), server_auth)


class TestEndToEnd(RemoteWriteServerMixin, unittest.TestCase):
    """cron 模式完整跑 main()，一个 job 配 prometheus_url，一个不配"""

    def setUp(self):
        super().setUp()
        self.conf_dir = os.path.join(self.tmp.name, 'conf')
        self.out_dir = os.path.join(self.tmp.name, 'out')
        self.buffer_dir = os.path.join(self.tmp.name, 'buffer')
        self.state_dir = os.path.join(self.tmp.name, 'state')
        os.mkdir(self.conf_dir)
        os.mkdir(self.out_dir)
        self.prefix = 't' + uuid.uuid4().hex[:8]
        with open(os.path.join(self.conf_dir, self.prefix + '.ini'), 'w') as f:
            f.write('[rw]\nname: rw\nurl: {0}/metrics\ninterval: 30\n'
                    'prometheus_url: {0}/api/v1/write\n'
                    '[local]\nname: local\nurl: {0}/metrics\ninterval: 30\n'.format(self.base_url))

    def run_main(self):
        asyncio.run(pt.main(os.path.join(self.conf_dir, '*.ini'), self.out_dir, False, self.buffer_dir,
                            self.state_dir))

    def status(self):
        with open(os.path.join(self.out_dir, self.prefix + '_rw_remote_write.prom')) as f:
            return {labels['__name__']: v for labels, v in pt.parse_samples(f.read())}

    def test_textfile_only_for_local_job(self):
        self.run_main()
        self.assertEqual(sorted(os.listdir(self.out_dir)), sorted([
            'prom-textfile.prom', self.prefix + '_local.prom', self.prefix + '_rw_remote_write.prom']))

    def test_remote_series(self):
        self.run_main()
        series = self.sent()[0]
        by_name = {dict(labels)['__name__']: (dict(labels), v) for labels, v, _ in series}
        labels, v = by_name['m']
        self.assertEqual(labels['a'], 'x "y"')
        self.assertEqual(labels['prom_cronjob_name'], 'rw')
        self.assertEqual(v, 1.0)
        self.assertEqual(by_name['prom_textfile_job_up'][1], 1.0)
        self.assertEqual(by_name['prom_textfile_job_count'][1], 1.0)
        self.assertEqual(self.status(), {'prom_textfile_remote_write_up': 1.0,
                                         'prom_textfile_remote_write_buffer_files': 0.0,
                                         'prom_textfile_remote_write_rejected_files': 0.0})

    def test_outage_then_backfill(self):
        _RemoteWriteHandler.codes = [503, 503]
        self.run_main()
        self.run_main()
        self.assertEqual(self.status()['prom_textfile_remote_write_up'], 0.0)
        self.assertEqual(self.status()['prom_textfile_remote_write_buffer_files'], 2.0)
        _RemoteWriteHandler.requests = []
        self.run_main()
        self.assertEqual(self.status()['prom_textfile_remote_write_up'], 1.0)
        self.assertEqual(self.status()['prom_textfile_remote_write_buffer_files'], 0.0)
        counts = []
        for series in self.sent():
            for labels, v, _ in series:
                if dict(labels)['__name__'] == 'prom_textfile_job_count':
                    counts.append(v)
        self.assertEqual(counts, [1.0, 2.0, 3.0])


if __name__ == '__main__':
    unittest.main()
