"""Скрипт, который Claude вызывает как хук. Только stdlib — запускается на каждое событие.

Использование: python hook.py <socket> <session-key> <event>
Читает JSON события со stdin, отправляет в сервис по unix-сокету, печатает ответ сервиса.
Если сервис недоступен — молча выходит, и Claude ведёт себя как обычно.
"""

import http.client
import socket
import sys


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=None)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)


def main() -> None:
    sock_path, key, event = sys.argv[1:4]
    payload = sys.stdin.buffer.read()
    try:
        conn = UnixHTTPConnection(sock_path)
        conn.request("POST", f"/hook/{event}?key={key}", body=payload, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
    except OSError:
        return
    if resp.status == 200 and body.strip():
        sys.stdout.buffer.write(body)


if __name__ == "__main__":
    main()
