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


def make_metrics(daemon=False, interval='30', **extra):
    conf = {'name': 'job', 'url': 'http://127.0.0.1:1/metrics',
            'interval': interval, 'prom_file_name': 'x_job', 'daemon': daemon}
    conf.update(extra)
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


class TestCustomLabels(unittest.TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def parse(self, text):
        return pt.parse_custom_labels(text, 'test')

    def recombine(self, line, **conf):
        p = make_metrics(**conf)
        return p._prom_metrics__recombine_line(line)

    def load(self, text, name='a.ini'):
        with open(os.path.join(self.tmp.name, name), 'w') as f:
            f.write(text)
        data = pt.config(os.path.join(self.tmp.name, name)).config_files()
        return {d['prom_file_name']: d for d in data}

    # parse_custom_labels

    def test_parse_basic(self):
        self.assertEqual(self.parse('env=prod,region=us-east'), {'env': 'prod', 'region': 'us-east'})

    def test_parse_spaces_and_trailing_comma(self):
        self.assertEqual(self.parse(' env = prod , region = us-east , '), {'env': 'prod', 'region': 'us-east'})

    def test_parse_quoted(self):
        self.assertEqual(self.parse(r'team="a,b",q="say \"hi\"",p="c:\\d"'),
                         {'team': 'a,b', 'q': 'say "hi"', 'p': 'c:\\d'})

    def test_parse_special_chars_unquoted(self):
        self.assertEqual(self.parse('pct=50%,host=主机,url=http://x:1/a?b=c'),
                         {'pct': '50%', 'host': '主机', 'url': 'http://x:1/a?b=c'})

    def test_parse_empty(self):
        self.assertEqual(self.parse(''), {})
        self.assertEqual(self.parse('dc='), {'dc': ''})

    def test_parse_reserved_and_invalid_names_skipped(self):
        self.assertEqual(self.parse('__x=1,prom_cronjob_name=x,1bad=1,ok=1'), {'ok': '1'})

    def test_parse_garbage_stops(self):
        self.assertEqual(self.parse('ok=1,novalue,later=2'), {'ok': '1'})
        self.assertEqual(self.parse('a="x"y,b=1'), {})

    # config 合并

    def test_config_default_and_job_labels(self):
        conf = self.load('[DEFAULT]\nlabels : env=prod,dc=sh\ninterval : 15\n'
                         '[node]\nname : node\nurl : http://x/1\n'
                         '[mysql]\nname : mysql\nurl : http://x/2\ninterval : 30\nlabels : env=staging,role=db\n'
                         '[redis]\nname : redis\nurl : http://x/3\nlabels : dc=\n')
        self.assertEqual(sorted(conf), ['a_mysql', 'a_node', 'a_redis'])
        self.assertEqual(conf['a_node']['custom_labels'], {'env': 'prod', 'dc': 'sh'})
        self.assertEqual(conf['a_node']['interval'], '15')
        self.assertEqual(conf['a_mysql']['custom_labels'], {'env': 'staging', 'dc': 'sh', 'role': 'db'})
        self.assertEqual(conf['a_mysql']['interval'], '30')
        self.assertEqual(conf['a_redis']['custom_labels'], {'env': 'prod'})

    def test_config_default_only_is_not_a_job(self):
        self.assertEqual(self.load('[DEFAULT]\nlabels : env=prod\n'), {})

    def test_config_without_labels(self):
        conf = self.load('[node]\nname : node\nurl : http://x/1\ninterval : 30\n')
        self.assertEqual(conf['a_node']['custom_labels'], {})

    def test_config_percent_in_values(self):
        conf = self.load('[node]\nname : node\nurl : http://x/a%20b\ninterval : 30\nlabels : pct=50%\n')
        self.assertEqual(conf['a_node']['url'], 'http://x/a%20b')
        self.assertEqual(conf['a_node']['custom_labels'], {'pct': '50%'})

    # 写出

    def test_custom_labels_added(self):
        self.assertEqual(
            self.recombine('m{a="1"} 2', custom_labels={'env': 'prod'}),
            'm{a="1",env="prod",prom_cronjob_interval="60",prom_cronjob_name="job",'
            'prom_cronjob_url="http://127.0.0.1:1/metrics"} 2\n')

    def test_custom_label_value_escaped(self):
        line = self.recombine('m 1', custom_labels={'q': 'say "hi"\\n'})
        self.assertIn(r'q="say \"hi\"\\n"', line)
        self.assertEqual(pt.parse_samples(line)[0][0]['q'], 'say "hi"\\n')

    def test_conflict_exported_is_default(self):
        line = self.recombine('m{env="dev"} 1', custom_labels={'env': 'prod'})
        self.assertIn('env="prod"', line)
        self.assertIn('exported_env="dev"', line)

    def test_conflict_exported_twice(self):
        line = self.recombine('m{env="dev",exported_env="old"} 1', custom_labels={'env': 'prod'},
                              labels_conflict='exported')
        self.assertIn('{env="prod"', line)
        self.assertIn('exported_env="old"', line)
        self.assertIn('exported_exported_env="dev"', line)

    def test_conflict_overwrite(self):
        line = self.recombine('m{env="dev"} 1', custom_labels={'env': 'prod'}, labels_conflict='overwrite')
        self.assertIn('env="prod"', line)
        self.assertNotIn('dev', line)

    def test_conflict_invalid_falls_back_to_exported(self):
        line = self.recombine('m{env="dev"} 1', custom_labels={'env': 'prod'}, labels_conflict='bad')
        self.assertIn('exported_env="dev"', line)


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
        self.state_dir = os.path.join(self.tmp.name, 'state')
        os.mkdir(self.conf_dir)
        os.mkdir(self.out_dir)
        self.prefix = 't' + uuid.uuid4().hex[:8]
        with open(os.path.join(self.conf_dir, self.prefix + '.ini'), 'w') as f:
            f.write('[ok]\nname: ok\nurl: {0}/ok\ninterval: 30\n'
                    '[down]\nname: down\nurl: http://127.0.0.1:1/\ninterval: 30\n'
                    .format(self.base_url))

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def run_main(self):
        asyncio.run(pt.main(os.path.join(self.conf_dir, '*.ini'), self.out_dir, False,
                            os.path.join(self.tmp.name, 'buffer'), self.state_dir))

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

    def test_counters_in_state_dir(self):
        self.run_main()
        self.assertEqual(sorted(os.listdir(self.state_dir)), sorted([
            'prom-textfile-self.txt', self.prefix + '_ok.txt', self.prefix + '_down.txt']))

    def test_cron_counter_increments(self):
        self.run_main()
        self.run_main()
        self.assertRegex(self.read('ok'), r'\nprom_textfile_job_count\{[^}]*\} 2\n')


class TestCustomLabelsEndToEnd(ServerMixin, unittest.TestCase):
    """cron 模式完整跑一次 main()，[DEFAULT] 和 job 的 labels、两种冲突处理"""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.conf_dir = os.path.join(self.tmp.name, 'conf')
        self.out_dir = os.path.join(self.tmp.name, 'out')
        os.mkdir(self.conf_dir)
        os.mkdir(self.out_dir)
        with open(os.path.join(self.conf_dir, 'c.ini'), 'w') as f:
            f.write('[DEFAULT]\nlabels : env=prod,dc=sh\ninterval : 30\n'
                    '[ok]\nname : ok\nurl : {0}/ok\nlabels : a=custom\n'
                    '[over]\nname : over\nurl : {0}/ok\nlabels : a=custom\nlabels_conflict : overwrite\n'
                    '[down]\nname : down\nurl : http://127.0.0.1:1/\nlabels : dc=\n'.format(self.base_url))
        asyncio.run(pt.main(os.path.join(self.conf_dir, '*.ini'), self.out_dir, False,
                            os.path.join(self.tmp.name, 'buffer'), os.path.join(self.tmp.name, 'state')))

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def samples(self, name):
        with open(os.path.join(self.out_dir, name)) as f:
            return [labels for labels, _ in pt.parse_samples(f.read())]

    def find(self, name, metric, **match):
        for labels in self.samples(name):
            if labels['__name__'] == metric and all(labels.get(k) == v for k, v in match.items()):
                return labels
        self.fail('{} {} not found in {}'.format(metric, match, name))

    def test_no_default_job_file(self):
        self.assertEqual(sorted(os.listdir(self.out_dir)),
                         ['c_down.prom', 'c_ok.prom', 'c_over.prom', 'prom-textfile.prom'])

    def test_scraped_metrics_exported(self):
        m = self.find('c_ok.prom', 'm', b='x y')
        self.assertEqual((m['a'], m['exported_a'], m['env'], m['dc']), ('custom', '1', 'prod', 'sh'))
        # 原来没有 a 的 metric 只加 label，不产生 exported_a
        plain = [l for l in self.samples('c_ok.prom') if l['__name__'] == 'm' and 'b' not in l][0]
        self.assertEqual(plain['a'], 'custom')
        self.assertNotIn('exported_a', plain)

    def test_scraped_metrics_overwrite(self):
        m = self.find('c_over.prom', 'm', b='x y')
        self.assertEqual(m['a'], 'custom')
        self.assertNotIn('exported_a', m)

    def test_job_metrics_get_labels(self):
        for name in ('c_ok.prom', 'c_down.prom'):
            names = set()
            for labels in self.samples(name):
                if labels['__name__'].startswith('prom_textfile_job_'):
                    names.add(labels['__name__'])
                    self.assertEqual(labels['env'], 'prod', (name, labels))
            self.assertEqual(names, {'prom_textfile_job_up', 'prom_textfile_job_count'})

    def test_job_unsets_default_label(self):
        for labels in self.samples('c_down.prom'):
            self.assertNotIn('dc', labels)

    def test_self_metrics_without_labels(self):
        for labels in self.samples('prom-textfile.prom'):
            self.assertNotIn('env', labels)


class TestCounter(unittest.TestCase):

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.count = pt.monitoring(False)._monitoring__count_with_file

    def tearDown(self):
        self.tmp.cleanup()
        logging.disable(logging.NOTSET)

    def test_creates_dir_and_increments(self):
        path = os.path.join(self.tmp.name, 'a', 'b', 'c.txt')
        self.assertEqual([self.count(path, 'x') for _ in range(3)], [1, 2, 3])

    def test_invalid_content_resets(self):
        path = os.path.join(self.tmp.name, 'c.txt')
        with open(path, 'w') as f:
            f.write('abc')
        self.assertEqual(self.count(path, 'x'), 1)

    def test_unwritable_does_not_raise(self):
        # 父路径是普通文件，root 也无法写入
        blocker = os.path.join(self.tmp.name, 'file')
        open(blocker, 'w').close()
        self.assertEqual(self.count(os.path.join(blocker, 'c.txt'), 'x'), 1)


class TestCli(unittest.TestCase):

    def run_cli(self, d, *args):
        return subprocess.run([sys.executable, SCRIPT, '-s', os.path.join(d, 'state'),
                               '-b', os.path.join(d, 'buffer')] + list(args), capture_output=True)

    def write_conf(self, d):
        path = os.path.join(d, 'a.ini')
        with open(path, 'w') as f:
            f.write('[n]\nname: n\nurl: http://127.0.0.1:1/\ninterval: 30\n')
        return path

    def test_exit_code_on_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.write_conf(d)
            r = self.run_cli(d, '-c', d, '-p', os.path.join(d, 'missing'))
            self.assertEqual(r.returncode, 1)
            self.assertIn(b'Traceback', r.stderr)

    def test_config_path_dir_or_file(self):
        for use_file in (False, True):
            with self.subTest(use_file=use_file), tempfile.TemporaryDirectory() as d:
                conf = self.write_conf(d)
                out = os.path.join(d, 'out')
                os.mkdir(out)
                r = self.run_cli(d, '-c', conf if use_file else d, '-p', out)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('a_n.prom', os.listdir(out))

    def test_config_glob(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, 'a.ini')
            open(conf, 'w').close()
            self.assertEqual(pt.config_glob(conf), conf)
            self.assertEqual(pt.config_glob(d), os.path.join(d, '*.ini'))


if __name__ == '__main__':
    unittest.main()
