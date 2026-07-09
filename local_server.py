#!/usr/bin/env python3
"""
Balls Music — локальный сервер музыки.

Запускается на вашем ПК, где хранится музыка. Раздаёт:
  - GET /manifest.json  -> актуальный список треков (JSON), собирается на лету
  - GET /music/<путь>   -> сам аудиофайл (с поддержкой перемотки / Range-запросов)

Сайт на хостинге обращается сюда через Cloudflare Tunnel — сам сервер слушает
только localhost:8080, наружу в интернет его "публикует" cloudflared.

Использование:
1. Положите этот файл рядом с папкой "music" (там ваша музыка, можно с подпапками).
2. Запустите:  python local_server.py
3. Не закрывайте окно, пока хотите слушать музыку через сайт.
"""

import http.server
import socketserver
import os
import re
import json
import mimetypes
from urllib.parse import unquote

MUSIC_DIR = "music"
PORT = 8080
EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".weba", ".webm"}

# Можно сузить до адреса вашего сайта вместо "*" для дополнительной строгости,
# например: "https://ваш-сайт.infinityfreeapp.com"
ALLOWED_ORIGIN = "*"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._handle(send_body=True)

    def do_HEAD(self):
        self._handle(send_body=False)

    def _handle(self, send_body):
        path = self.path.split("?")[0]
        if path in ("/manifest.json", "/"):
            self.serve_manifest(send_body)
        elif path.startswith("/music/"):
            self.serve_file(path[len("/music/"):], send_body)
        else:
            self.send_error(404, "Not found")

    def serve_manifest(self, send_body=True):
        tracks = []
        if os.path.isdir(MUSIC_DIR):
            for root, _dirs, files in os.walk(MUSIC_DIR):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in EXTS:
                        rel = os.path.relpath(os.path.join(root, f), MUSIC_DIR).replace(os.sep, "/")
                        tracks.append(rel)
        tracks.sort(key=str.lower)
        body = json.dumps(tracks, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def serve_file(self, rel, send_body=True):
        rel = unquote(rel)
        full = os.path.normpath(os.path.join(MUSIC_DIR, rel))
        base = os.path.normpath(MUSIC_DIR)
        if not full.startswith(base) or not os.path.isfile(full):
            self.send_error(404, "File not found")
            return

        file_size = os.path.getsize(full)
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")

        if range_header:
            m = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self._cors()
                self.end_headers()
                if send_body:
                    with open(full, "rb") as fh:
                        fh.seek(start)
                        remaining = length
                        chunk = 65536
                        while remaining > 0:
                            data = fh.read(min(chunk, remaining))
                            if not data:
                                break
                            self.wfile.write(data)
                            remaining -= len(data)
                return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self._cors()
        self.end_headers()
        if send_body:
            with open(full, "rb") as fh:
                while True:
                    data = fh.read(65536)
                    if not data:
                        break
                    self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # тише в консоли


if __name__ == "__main__":
    if not os.path.isdir(MUSIC_DIR):
        print(f"Папка '{MUSIC_DIR}' не найдена рядом со скриптом.")
        print("Создайте папку 'music' и положите в неё аудиофайлы, затем запустите скрипт снова.")
        raise SystemExit(1)

    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Сервер музыки запущен: http://127.0.0.1:{PORT}")
        print("Список треков:  http://127.0.0.1:%d/manifest.json" % PORT)
        print("Не закрывайте это окно, пока хотите слушать музыку через сайт.")
        print("Дальше запустите cloudflared, чтобы опубликовать этот адрес в интернет.")
        httpd.serve_forever()
