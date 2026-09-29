"""Скрипт, который Claude вызывает как хук. Только stdlib — запускается на каждое событие.

Использование: python hook.py <socket> <session-key> <event>
Читает JSON события со stdin, отправляет в сервис по unix-сокету, печатает ответ сервиса.
Если сервис перезапускается (автообновление), ждём его и повторяем запрос: иначе вопрос или
запрос разрешения, на который пользователь ещё не ответил, уйдёт в терминал, где его никто не видит.
Если сервис так и не поднялся — молча выходим, и Claude ведёт себя как обычно.
"""

import http.client
import socket
import sys
import time

# сколько ждать, пока сервис поднимется: Stop-хук ограничен 60с таймаутом в настройках Claude
RETRY_FOR = {"Stop": 30.0}
DEFAULT_RETRY_FOR = 180.0


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
    retry_for = RETRY_FOR.get(event, DEFAULT_RETRY_FOR)
    failing_since = None  # отсчёт ожидания — от обрыва, а не от начала хука (ответа могли ждать час)
    while True:
        try:
            conn = UnixHTTPConnection(sock_path)
            conn.request("POST", f"/hook/{event}?key={key}", body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            break
        except OSError:  # сервис недоступен или перезапустился, пока мы ждали ответа пользователя
            now = time.monotonic()
            failing_since = failing_since or now
            if now - failing_since >= retry_for:
                return
            time.sleep(2)
    if resp.status == 200 and body.strip():
        sys.stdout.buffer.write(body)


if __name__ == "__main__":
    main()
