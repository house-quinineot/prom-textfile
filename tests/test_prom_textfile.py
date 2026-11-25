#!/usr/bin/env python3
"""
prom-textfile 测试，只用标准库。

运行: python3 -m unittest discover -s tests -v
"""

import asyncio
import http.server
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'prom-textfile.py')

# 文件名带 "-"，不能直接 import
_spec = importlib.util.spec_from_file_location('prom_textfile', SCRIPT)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)


SAMPLE_METRICS = '''# HELP m test
# TYPE m gauge
m 1
m{a="1",b="x y"} 2
ns:rule_total{job="j"} 3
h_bucket{le="+Inf"} 4
neg -Inf
nan NaN
'''


class _Handler(http.server.BaseHTTPRequestHandler):
    """/ok 返回 SAMPLE_METRICS，/slow 延迟 1s，/err 返回 503"""

    def do_GET(self):
        if self.path == '/slow':
            time.sleep(1)
        if self.path == '/err':
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(SAMPLE_METRICS.encode())

    def log_message(self, *args):
        pass


class ServerMixin:

    @classmethod
    def setUpClass(cls):
        http.server.ThreadingHTTPServer.allow_reuse_address = True
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        cls.base_url = 'http://127.0.0.1:{}'.format(cls.server.server_address[1])
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


def make_metrics(daemon=False, interval='30'):
    conf = {'name': 'job', 'url': 'http://127.0.0.1:1/metrics',
            'interval': interval, 'prom_file_name': 'x_job', 'daemon': daemon}
    p = pt.prom_metrics(conf, '/nonexistent')
    p.get_timestamp = str(int(time.time()))
    return p


class TestConfig(unittest.TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def write(self, name, text):
        with open(os.path.join(self.tmp.name, name), 'w') as f:
            f.write(text)

    def test_multi_file_multi_section(self):
        self.write('a.ini', '[n1]\nname: n1\nurl: http://x/1\ninterval: 30\n'
                            '[n2]\nname: n2\nurl: http://x/2\ninterval: 10\n')
        self.write('b.ini', '[n3]\nname: n3\nurl: http://x/3\ninterval: 5\n')
        data = pt.config(os.path.join(self.tmp.name, '*.ini')).config_files()
        names = sorted(d['prom_file_name'] for d in data)
        self.assertEqual(names, ['a_n1', 'a_n2', 'b_n3'])

    def test_file_name_without_ini_suffix(self):
        self.write('svc.conf', '[n1]\nname: n1\nurl: http://x/1\ninterval: 30\n')
        data = pt.config(os.path.join(self.tmp.name, '*.conf')).config_files()
        self.assertEqual(data[0]['prom_file_name'], 'svc_n1')


class TestParse(unittest.TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.p = make_metrics()

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def label_to_dict(self, s):
        return self.p._prom_metrics__label_to_dict(s)

    def metric_dict(self, s):
        return self.p._prom_metrics__metric_dict(s)

    def recombine(self, s):
        return self.p._prom_metrics__recombine_line(s)

    def test_labels_multiple(self):
        self.assertEqual(self.label_to_dict('a="1",b="x y"'), {'a': '1', 'b': 'x y'})

    def test_labels_comma_and_escaped_quote_in_value(self):
        self.assertEqual(self.label_to_dict(r'a="x,y",b="say \"hi\""'),
                         {'a': 'x,y', 'b': r'say \"hi\"'})

    def test_metric_simple(self):
        d = self.metric_dict('m 1')
        self.assertEqual((d['name'], d['value'], d['label']), ('m', '1', {}))

    def test_metric_colon_name(self):
        d = self.metric_dict('ns:rule_total{job="j"} 3')
        self.assertEqual(d['name'], 'ns:rule_total')
        self.assertEqual(d['label'], {'job': 'j'})

    def test_metric_special_values(self):
        for v in ('+Inf', '-Inf', 'NaN', '1.5e-07', '-3'):
            with self.subTest(v=v):
                self.assertEqual(self.metric_dict('m ' + v)['value'], v)

    def test_metric_label_value_with_inf(self):
        d = self.metric_dict('h_bucket{le="+Inf"} 4')
        self.assertEqual(d['label'], {'le': '+Inf'})
        self.assertEqual(d['value'], '4')

    def test_metric_unparsable(self):
        self.assertEqual(self.metric_dict('{broken'), '')

    def test_timestamp_fresh_no_label(self):
        now_ms = str(int(time.time()) * 1000)
        self.assertNotIn('metric_timestamp', self.metric_dict('m 1 ' + now_ms))

    def test_timestamp_stale_ms(self):
        d = self.metric_dict('m 1 1600000000000')
        self.assertEqual(d['metric_timestamp'], '2020-09-13_12:26:40+00')

    def test_timestamp_stale_seconds(self):
        d = self.metric_dict('m 1 1600000000')
        self.assertEqual(d['metric_timestamp'], '1600000000')

    def test_recombine_comment_passthrough(self):
        self.assertEqual(self.recombine('# HELP m test'), '# HELP m test\n')

    def test_recombine_unparsable_skipped(self):
        self.assertEqual(self.recombine('{broken'), '')

    def test_recombine_adds_default_labels_sorted(self):
        self.assertEqual(
            self.recombine('m{b="2"} 1'),
            'm{b="2",prom_cronjob_interval="60",prom_cronjob_name="job",'
            'prom_cronjob_url="http://127.0.0.1:1/metrics"} 1\n')

    def test_interval_label_cron_is_60(self):
        self.assertEqual(make_metrics(daemon=False, interval='30')
                         .default_label['prom_cronjob_interval'], 60)

    def test_interval_label_daemon_is_config(self):
        self.assertEqual(make_metrics(daemon=True, interval='30')
                         .default_label['prom_cronjob_interval'], '30')


class TestWriteFileAtomic(unittest.TestCase):

    def test_write_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'a.prom')
            pt.write_file_atomic(path, ['a 1\n', 'b 2\n'])
            pt.write_file_atomic(path, ['c 3\n'])
            with open(path) as f:
                self.assertEqual(f.read(), 'c 3\n')
            self.assertEqual(os.listdir(d), ['a.prom'])


class TestFetch(ServerMixin, unittest.TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_ok(self):
        body, code = asyncio.run(pt.get_url(self.base_url + '/ok'))
        self.assertEqual((body, code), (SAMPLE_METRICS, 200))

    def test_http_error_keeps_code(self):
        self.assertEqual(asyncio.run(pt.get_url(self.base_url + '/err')), ('', 503))

    def test_connection_refused(self):
        self.assertEqual(asyncio.run(pt.get_url('http://127.0.0.1:1/')), ('', 500))

    def test_concurrent(self):
        async def three():
            return await asyncio.gather(
                *(pt.get_url(self.base_url + '/slow') for _ in range(3)))
        start = time.monotonic()
        asyncio.run(three())
        # 串行要 3s 以上
        self.assertLess(time.monotonic() - start, 2.5)


class TestEndToEnd(ServerMixin, unittest.TestCase):
    """cron 模式完整跑一次 main()"""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.conf_dir = os.path.join(self.tmp.name, 'conf')
        self.out_dir = os.path.join(self.tmp.name, 'out')
        os.mkdir(self.conf_dir)
        os.mkdir(self.out_dir)
        # cron 模式计数写在 /tmp/<prom_file_name>.txt，用唯一前缀避免互相影响
        self.prefix = 't' + uuid.uuid4().hex[:8]
        with open(os.path.join(self.conf_dir, self.prefix + '.ini'), 'w') as f:
            f.write('[ok]\nname: ok\nurl: {0}/ok\ninterval: 30\n'
                    '[down]\nname: down\nurl: http://127.0.0.1:1/\ninterval: 30\n'
                    .format(self.base_url))

    def tearDown(self):
        for job in ('ok', 'down'):
            try:
                os.remove('/tmp/{}_{}.txt'.format(self.prefix, job))
            except FileNotFoundError:
                pass
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def run_main(self):
        asyncio.run(pt.main(os.path.join(self.conf_dir, '*.ini'), self.out_dir, False))

    def read(self, job):
        with open(os.path.join(self.out_dir, '{}_{}.prom'.format(self.prefix, job))) as f:
            return f.read()

    def test_output_files(self):
        self.run_main()
        self.assertEqual(sorted(os.listdir(self.out_dir)),
                         sorted(['prom-textfile.prom',
                                 self.prefix + '_ok.prom', self.prefix + '_down.prom']))

    def test_ok_job(self):
        self.run_main()
        text = self.read('ok')
        for name in ('m{', 'ns:rule_total{', 'h_bucket{le="+Inf"', 'neg{', 'nan{'):
            self.assertIn('\n' + name, '\n' + text)
        self.assertRegex(text, r'\nprom_textfile_job_up\{[^}]*prom_cronjob_name="ok"[^}]*\} 1\n')
        self.assertRegex(text, r'\nprom_textfile_job_count\{[^}]*\} 1\n')

    def test_down_job(self):
        self.run_main()
        self.assertRegex(self.read('down'), r'prom_textfile_job_up\{[^}]*\} 0\n')

    def test_cron_counter_increments(self):
        self.run_main()
        self.run_main()
        self.assertRegex(self.read('ok'), r'\nprom_textfile_job_count\{[^}]*\} 2\n')


class TestCli(unittest.TestCase):

    def test_exit_code_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'a.ini'), 'w') as f:
                f.write('[n]\nname: n\nurl: http://127.0.0.1:1/\ninterval: 30\n')
            r = subprocess.run([sys.executable, SCRIPT, '-c', d, '-p', os.path.join(d, 'missing')],
                               capture_output=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b'Traceback', r.stderr)


if __name__ == '__main__':
    unittest.main()
