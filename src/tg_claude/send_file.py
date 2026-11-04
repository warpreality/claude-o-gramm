"""Команда send-file: Claude отправляет файл пользователю в чат. Только stdlib.

Использование (из сессии Claude): send-file <путь> [подпись]
Сокет сервиса и ключ сессии приходят через переменные окружения TGC_SOCKET и TGC_KEY —
их выставляет бот при запуске Claude. Печатает результат, при ошибке выходит с кодом 1.
"""

import http.client
import json
import os
import socket
import sys
import time

WAIT_SERVICE = 30.0  # сервис мог перезапускаться (автообновление) — недолго ждём


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=600)  # большой файл в Telegram может уходить долго
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)


def fail(msg: str) -> None:
    print(f"Ошибка: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Использование: send-file <путь к файлу> [подпись]")
        sys.exit(0 if len(sys.argv) > 1 else 1)
    sock, key = os.environ.get("TGC_SOCKET"), os.environ.get("TGC_KEY")
    if not sock or not key:
        fail("команда работает только внутри сессии, запущенной ботом")
    path = os.path.abspath(sys.argv[1])
    if not os.path.isfile(path):
        fail(f"файл не найден: {path}")
    payload = json.dumps({"path": path, "caption": " ".join(sys.argv[2:])}).encode()
    started = time.monotonic()
    while True:
        try:
            conn = UnixHTTPConnection(sock)
            conn.request("POST", f"/send-file?key={key}", body=payload, headers={"Content-Type": "application/json"})
            body = conn.getresponse().read()
            break
        except (ConnectionRefusedError, FileNotFoundError):  # сервис ещё не поднялся — можно повторить
            if time.monotonic() - started >= WAIT_SERVICE:
                fail("бот недоступен, файл не отправлен")
            time.sleep(2)
        except OSError as e:  # оборвалось на середине — не повторяем, чтобы файл не пришёл дважды
            fail(f"связь с ботом оборвалась ({e}); проверь, дошёл ли файл, прежде чем слать снова")
    try:
        result = json.loads(body or b"{}")
    except ValueError:
        result = {"error": body.decode(errors="replace")[:300]}
    if result.get("ok"):
        print(f"Файл {os.path.basename(path)} отправлен в чат.")
    else:
        fail(result.get("error") or "неизвестная ошибка")


if __name__ == "__main__":
    main()
