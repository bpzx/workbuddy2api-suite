"""端到端验收：OpenAI Responses API 兼容层（`/v1/responses`）。

为什么不能只靠单元测试：Responses 的价值几乎全在**流式**上，而流式的行为
（分帧、分块、headers）在 TestClient 与真实 uvicorn 之间可能不同。本脚本真起
一个 uvicorn + 一个假上游，用真 socket 打过去，并按**客户端解析器的规则**把
事件流拼装回文本——只有拼得出正确结果才算过。

这个脚本抓到过一个单元测试没抓到的真 bug：`stream` 标志没有转告上游，于是
上游回了 JSON、网关却按 SSE 解析，一个 data 帧都解不出来，客户端收到
「response.created + response.completed」的空回答（不报错、不重试）。

    python dev/verify_responses_e2e.py

仅用于本地验收，请勿在生产环境运行。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

UPSTREAM_PORT = 19461
GATEWAY_PORT = 19462
ADMIN_PW = 'e2e-responses-verify'
DATA_DIR = REPO.parent / '_responses_e2e_data'

# 假上游按 Chat Completions 协议回话；网关要把它们翻成 Responses 事件流。
# 含一段 reasoning_content，用来验证推理与正文被拆成两个 output item。
UPSTREAM_CHUNKS = [
    {'choices': [{'index': 0, 'delta': {'reasoning_content': '让我想想…'}}]},
    {'choices': [{'index': 0, 'delta': {'content': '你好'}}]},
    {'choices': [{'index': 0, 'delta': {'content': '，世界'}}]},
    {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
     'usage': {'prompt_tokens': 7, 'completion_tokens': 5}},
]
UPSTREAM_MODELS = {'object': 'list', 'data': [{'id': 'glm-5.2'}, {'id': 'global:gpt-5.6-sol'}]}


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._json(UPSTREAM_MODELS)

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(length)
        try:
            want_stream = bool(json.loads(raw or b'{}').get('stream'))
        except ValueError:
            want_stream = False

        if not want_stream:
            self._json({
                'choices': [{'index': 0,
                             'message': {'role': 'assistant', 'content': '你好，世界'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 5},
            })
            return

        self.send_response(200)
        self.send_header('content-type', 'text/event-stream')
        self.send_header('cache-control', 'no-cache')
        # 分块传输：逼网关自己按行拆帧（而不是一次性拿到整个 body）
        self.send_header('transfer-encoding', 'chunked')
        self.end_headers()
        for obj in UPSTREAM_CHUNKS:
            self._chunk(f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'.encode())
            time.sleep(0.02)
        self._chunk(b'data: [DONE]\n\n')
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def _chunk(self, payload: bytes):
        self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n')
        self.wfile.flush()


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', port)) == 0:
                return True
        time.sleep(0.2)
    return False


def parse_events(raw: str) -> tuple[str, list[dict], dict | None]:
    """按客户端解析器的槽位规则拼装事件流。

    复刻 `@earendil-works/pi-ai` 的 `openai-responses-shared.js`：delta 只喂给
    **已存在**的槽位（按 output_index），所以 index 错位会表现为「内容静默丢失」。
    """
    slots: dict[int, dict] = {}
    text = ''
    terminal: dict | None = None
    for frame in raw.split('\n\n'):
        name = data = None
        for line in frame.split('\n'):
            if line.startswith('event: '):
                name = line[7:].strip()
            elif line.startswith('data: '):
                data = line[6:].strip()
        if not name or not data:
            continue
        payload = json.loads(data)
        kind = payload.get('type')
        if kind == 'response.output_item.added':
            slots[payload['output_index']] = {'kind': payload['item']['type'], 'buf': ''}
        elif kind == 'response.output_text.delta':
            slot = slots.get(payload['output_index'])
            if slot is not None:
                slot['buf'] += payload['delta']
        elif kind in ('response.completed', 'response.incomplete', 'response.failed'):
            terminal = payload
    for index in sorted(slots):
        if slots[index]['kind'] == 'message':
            text += slots[index]['buf']
    return text, [slots[i] for i in sorted(slots) if slots[i]['kind'] != 'message'], terminal


def main() -> int:
    upstream = HTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db, users = DATA_DIR / 'manager.db', DATA_DIR / 'users.json'
    for path in (db, users):
        path.unlink(missing_ok=True)

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_DB': str(db),
        'WB_USERS_FILE': str(users),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(GATEWAY_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
    }
    proc = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'server.main:app',
         '--host', '127.0.0.1', '--port', str(GATEWAY_PORT), '--log-level', 'info'],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    ok = True
    try:
        if not wait_port(GATEWAY_PORT):
            print('网关未能启动')
            return 1
        time.sleep(0.8)

        import httpx
        # trust_env=False：装了系统代理（Windows 注册表）时 httpx 会把 127.0.0.1
        # 的请求也交给代理，代理回 502——那会把验收带偏（产品侧
        # config.http_client 本来就是 trust_env=False）。
        client = httpx.Client(base_url=f'http://127.0.0.1:{GATEWAY_PORT}',
                              timeout=30, trust_env=False)

        r = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PW})
        assert r.status_code == 200, f'登录失败 {r.status_code}: {r.text[:300]}'
        cookies = r.cookies

        r = client.post('/api/keys', json={'name': 'e2e', 'realm': ''}, cookies=cookies)
        assert r.status_code == 200, f'建密钥失败 {r.status_code}: {r.text[:300]}'
        token = r.json()['key']
        auth = {'Authorization': f'Bearer {token}'}

        # ── 1. 非流式 ──
        r = client.post('/v1/responses', headers=auth,
                        json={'model': 'glm-5.2', 'input': '你好'})
        assert r.status_code == 200, r.text
        obj = r.json()
        assert obj['object'] == 'response', obj
        assert obj['status'] == 'completed', obj
        assert obj['output_text'] == '你好，世界', obj
        assert obj['usage'] == {
            'input_tokens': 7,
            'input_tokens_details': {'cached_tokens': 0},
            'output_tokens': 5,
            'output_tokens_details': {'reasoning_tokens': 0},
            'total_tokens': 12,
        }, obj
        print('[非流式] ✓ object/status/文本/用量 全部正确')

        # ── 2. 流式（真 socket）──
        with client.stream('POST', '/v1/responses', headers=auth,
                           json={'model': 'glm-5.2', 'input': '你好', 'stream': True}) as resp:
            assert resp.status_code == 200, resp.status_code
            assert 'event-stream' in resp.headers.get('content-type', ''), dict(resp.headers)
            raw = ''.join(resp.iter_text())
        text, _tools, terminal = parse_events(raw)
        assert terminal is not None, '没有终止事件（客户端会直接抛错）'
        assert text == '你好，世界', f'客户端拼出的文本错误：{text!r}'
        assert terminal['response']['status'] == 'completed', terminal
        assert terminal['response']['model'] == 'glm-5.2', terminal
        assert terminal['response']['usage']['total_tokens'] == 12, terminal
        kinds = [i['type'] for i in terminal['response']['output']]
        assert kinds == ['reasoning', 'message'], f'输出项应为推理+正文两项：{kinds}'
        events = [ln[7:] for ln in raw.splitlines() if ln.startswith('event: ')]
        print(f'[流式] ✓ {len(events)} 个事件，客户端拼出 {text!r}，输出项 {kinds}')

        # ── 3. 版本隔离在 Responses 路径同样生效，且真实原因不被折叠 ──
        r = client.post('/api/keys', json={'name': 'e2e-cn', 'realm': 'cn'}, cookies=cookies)
        cn = r.json()['key']
        r = client.post('/v1/responses', headers={'Authorization': f'Bearer {cn}'},
                        json={'model': 'global:gpt-5.6-sol', 'input': 'hi'})
        assert r.status_code == 400, f'版本不匹配该回 400（401/403 会被客户端显示成密钥无效）：{r.status_code}'
        msg = r.json()['error']['message']
        assert 'global:' in msg, msg
        print(f'[版本隔离] ✓ HTTP {r.status_code}，报文可见：{msg}')

        print('\n全部通过')
    except AssertionError as exc:
        print(f'\n✗ 断言失败：{exc}')
        ok = False
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        ok = False
    finally:
        proc.terminate()
        try:
            out = proc.stdout.read().decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            out = ''
        if not ok:
            print('\n=== 网关日志（尾部）===')
            print('\n'.join(out.splitlines()[-40:]))
        upstream.shutdown()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
