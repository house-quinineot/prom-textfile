#!/usr/bin/env python3
"""
集成测试用：prometheus 自身不支持 bearer token 认证，用这个代理模拟需要 token 的 remote write 端点。
Authorization 不是 "Bearer <token>" 返回 401，否则原样转发到 prometheus 并返回它的状态码。

用法: python3 tests/bearer_proxy.py <监听端口> <prometheus 地址> <token>
"""

import http.server
import sys
import urllib.error
import urllib.request

PORT, UPSTREAM, TOKEN = int(sys.argv[1]), sys.argv[2].rstrip('/'), sys.argv[3]


class Handler(http.server.BaseHTTPRequestHandler):

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ('authorization', 'host', 'content-length')}
        request = urllib.request.Request(UPSTREAM + self.path, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                code, data = r.code, r.read()
        except urllib.error.HTTPError as e:
            with e:
                code, data = e.code, e.read()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
