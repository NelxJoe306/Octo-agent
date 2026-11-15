from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from typing import Literal, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from octo_coding.session import CodingSession


class TopicCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['create', 'select', 'rename']
    session_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(default='main', min_length=1, max_length=128)
    title: str = Field(default='', max_length=120)
    anchor_entry_id: str | None = Field(default=None, max_length=128)
    activate: bool = False


class TopicWeb:
    def __init__(self, session: CodingSession, *, directory: Path | None = None, port: int = 0) -> None:
        directory = directory or Path(__file__).resolve().parents[2] / 'web' / 'out'
        if not (directory / 'index.html').is_file():
            raise ValueError('请先在 web 目录执行 npm run build')
        self.session = session
        self.loop = asyncio.get_running_loop()
        self.token = secrets.token_urlsafe(32)
        self.condition = threading.Condition()
        self.revision = 1
        self.closed = False
        self.server = ThreadingHTTPServer(('127.0.0.1', port), partial(_Handler, owner=self, directory=str(directory)))
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.url = self.origin + '/#token=' + self.token
        self.unsubscribe = session.subscribe_topics(self.changed)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()

    def changed(self) -> None:
        with self.condition:
            self.revision += 1
            self.condition.notify_all()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.unsubscribe()
        with self.condition:
            self.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, owner: TopicWeb, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    def handle(self) -> None:
        try:
            super().handle()
        except ConnectionError:
            pass

    def log_message(self, *_args) -> None:
        pass

    def _json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(payload)

    def _allowed(self) -> bool:
        token = self.headers.get('X-Octo-Token') or parse_qs(urlsplit(self.path).query).get('token', [''])[0]
        if (self.headers.get('Host') != urlsplit(self.owner.origin).netloc or
                self.headers.get('Origin', self.owner.origin) != self.owner.origin or
                not secrets.compare_digest(token, self.owner.token)):
            self._json(403, {'error': '请通过终端 /map 打开的地址连接'})
            return False
        return True

    def _call(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.owner.loop)
        try:
            return future.result(timeout=15)
        except FutureTimeout:
            raise ValueError('操作尚未确认，请刷新检查结果后再操作') from None

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if not parsed.path.startswith('/api/'):
            if self.headers.get('Host') != urlsplit(self.owner.origin).netloc:
                self.send_error(403)
                return
            return super().do_GET()
        if not self._allowed():
            return
        try:
            if parsed.path == '/api/events':
                self._events()
            elif parsed.path == '/api/snapshot':
                args = parse_qs(parsed.query)
                if 'before' in args and 'after' in args:
                    raise ValueError('Use only one cursor direction')
                before = int(args.get('before', ['9223372036854775807'])[0])
                after = int(args['after'][0]) if 'after' in args else None
                if not 0 < before <= 9223372036854775807 or after is not None and not 0 <= after <= 9223372036854775807:
                    raise ValueError('Invalid cursor')
                data = self._call(self.owner.session.topic_snapshot(args.get('topic_id', [None])[0], before, after=after))
                self._json(200, data)
            elif parsed.path == '/api/anchor':
                args = parse_qs(parsed.query)
                data = self._call(self.owner.session.locate_topic_entry(args.get('entry_id', [''])[0]))
                self._json(200, data)
            else:
                self._json(404, {'error': 'Unknown map endpoint'})
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
        except ConnectionError:
            pass
        except Exception:
            self._json(500, {'error': '无法读取话题，请查看终端'})

    def do_POST(self) -> None:
        if not self._allowed():
            return
        try:
            if urlsplit(self.path).path != '/api/topics':
                self._json(404, {'error': 'Unknown map endpoint'})
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('Invalid request size')
            command = TopicCommand.model_validate_json(self.rfile.read(length))
            topic_id = self._call(self.owner.session.edit_topic(**command.model_dump()))
            self._json(200, {'topic_id': topic_id})
        except ValidationError:
            self._json(400, {'error': 'Invalid topic command'})
        except ValueError as exc:
            self._json(400, {'error': str(exc)})
        except Exception:
            self._json(500, {'error': '操作未确认，请刷新检查结果并查看终端'})

    def _events(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        last = -1
        while not self.owner.closed:
            with self.owner.condition:
                if last == self.owner.revision:
                    self.owner.condition.wait(timeout=5)
                revision = self.owner.revision
            if self.owner.closed:
                break
            payload = f'data: {revision}\n\n' if revision != last else ': heartbeat\n\n'
            self.wfile.write(payload.encode())
            self.wfile.flush()
            last = revision
