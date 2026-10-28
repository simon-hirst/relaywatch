"""Best-effort immediate guidance to a watchdog-owned Codex daemon turn.

Only contacts the current user's local Unix socket. Explicit server receipts
are required: a successful queued message is not an immediate steer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time


class UncertainDelivery(Exception):
    """The request was sent but the server did not confirm its outcome."""


class NotSteerable(Exception):
    """The current thread has no verified active turn, or no reachable daemon."""


def control_socket(environ=None, home=None):
    env = os.environ if environ is None else environ
    root = Path(env.get('CODEX_HOME') or (Path.home() if home is None else Path(home)) / '.codex')
    path = root / 'app-server-control' / 'app-server-control.sock'
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return None
    except (OSError, AttributeError):
        return None
    return path


class _UnixWebSocket:
    def __init__(self, path, timeout=3):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            handshake = ('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                         'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key +
                         '\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self.sock.sendall(handshake.encode('ascii'))
            headers = bytearray()
            while b'\r\n\r\n' not in headers:
                chunk = self.sock.recv(1024)
                if not chunk or len(headers) + len(chunk) > 8192:
                    raise NotSteerable('invalid websocket handshake')
                headers.extend(chunk)
            head, _, extra = headers.partition(b'\r\n\r\n')
            if not head.startswith(b'HTTP/1.1 101 ') and not head.startswith(b'HTTP/1.0 101 '):
                raise NotSteerable('daemon rejected websocket connection')
            lines = head.decode('latin1').split('\r\n')[1:]
            fields = dict(line.split(':', 1) for line in lines if ':' in line)
            fields = {k.strip().lower(): v.strip() for k,v in fields.items()}
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if fields.get('sec-websocket-accept') != expected:
                raise NotSteerable('websocket accept validation failed')
            self.buffer = extra
        except Exception:
            self.sock.close()
            raise

    def close(self):
        self.sock.close()

    def _read(self, length):
        while len(self.buffer) < length:
            chunk = self.sock.recv(max(4096, length - len(self.buffer)))
            if not chunk:
                raise ConnectionError('Codex daemon closed websocket')
            self.buffer += chunk
        result, self.buffer = self.buffer[:length], self.buffer[length:]
        return result

    def _frame(self, data, opcode=1):
        mask = os.urandom(4)
        n = len(data)
        prefix = bytes([0x80 | opcode, 0x80 | n]) if n < 126 else (
            bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('!H', n)) if n < 65536 else (
            bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('!Q', n))
        self.sock.sendall(prefix + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(data)))

    def send(self, message):
        self._frame(json.dumps(message, separators=(',', ':')).encode('utf-8'))

    def recv(self):
        for _ in range(64):
            head = self._read(2)
            opcode = head[0] & 0x0f
            if (head[0] & 0x80) == 0:  # no fragmented messages
                raise NotSteerable('fragmented websocket reply')
            size = head[1] & 0x7f
            if size == 126:
                size = struct.unpack('!H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._read(8))[0]
            if size > 1_048_576:
                raise NotSteerable('oversized daemon reply')
            # Servers should not mask their frames.
            if head[1] & 0x80:
                raise NotSteerable('masked server frame')
            body = self._read(size)
            if opcode == 9:
                self._frame(body, 10)
                continue
            if opcode == 8:
                raise ConnectionError('daemon closed connection')
            if opcode == 1:
                return json.loads(body.decode('utf-8'))
        raise NotSteerable('too many daemon control frames')

    def request(self, request_id, method, params):
        self.send({'id': request_id, 'method': method, 'params': params})
        for _ in range(150):
            reply = self.recv()
            if reply.get('id') == request_id:
                return reply
        raise NotSteerable('no matching daemon response')


def _active_turn(payload):
    """Require an unambiguous currently running turn, never guess from history."""
    result = payload.get('result') if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise NotSteerable('Codex turn list unavailable')
    turns = result.get('data', result.get('turns'))
    if not isinstance(turns, list):
        raise NotSteerable('Codex turn list unsupported')
    active = [t['id'] for t in turns if isinstance(t, dict) and
              t.get('status') in ('inProgress', 'in_progress') and
              isinstance(t.get('id'), str)]
    if len(active) != 1:
        raise NotSteerable('no unique active Codex turn')
    return active[0]


def steer(thread, message, *, socket_path=None, client_factory=_UnixWebSocket):
    """Return True only after explicit turn/steer success; False if safe to queue.

    A lost response after sending turn/steer is *uncertain*, not permission to
    queue a duplicate. The caller must record that event and stop retries.
    """
    path = control_socket() if socket_path is None else socket_path
    if path is None:
        return False
    try:
        conn = client_factory(path)
    except (OSError, NotSteerable):
        return False
    try:
        response = conn.request(1, 'initialize', {
            'clientInfo': {'name': 'relaywatch', 'title': 'RelayWatch', 'version': '3.18'}})
        if 'error' in response or not isinstance(response.get('result'), dict):
            return False
        conn.send({'method': 'initialized', 'params': {}})
        turns = conn.request(2, 'thread/turns/list', {'threadId': thread, 'limit': 10, 'itemsView': 'none'})
        try:
            active = _active_turn(turns)
        except NotSteerable:
            return False
        # From this point the daemon might consume the instruction.
        try:
            response = conn.request(3, 'turn/steer', {
                'threadId': thread, 'expectedTurnId': active,
                'input': [{'type': 'text', 'text': message}]})
        except (OSError, ConnectionError, TimeoutError, NotSteerable) as exc:
            raise UncertainDelivery(f'Codex steer acknowledgement unavailable: {type(exc).__name__}') from exc
        if 'error' in response:
            return False
        result = response.get('result')
        if not isinstance(result, dict) or result.get('turnId') != active:
            raise UncertainDelivery('unexpected Codex steer acknowledgement')
        return True
    except (OSError, ConnectionError, TimeoutError, NotSteerable):
        return False
    finally:
        conn.close()
