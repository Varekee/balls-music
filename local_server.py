#!/usr/bin/env python3
"""
Balls Music — локальный сервер музыки (с тегами, обложками и избранным).

Запускается на вашем ПК, где хранится музыка. Раздаёт:
  - GET  /manifest.json   -> список треков с тегами и отметкой избранного, JSON
  - GET  /music/<путь>    -> сам аудиофайл (с поддержкой перемотки / Range-запросов)
  - GET  /art/<путь>      -> обложка, встроенная в файл (если есть)
  - POST /favorite        -> добавить/убрать трек из избранного, JSON {"rel": "...", "favorite": true}

Все запросы (кроме OPTIONS) требуют параметр ?key=ВАШ_КОД — см. ACCESS_KEY ниже.

Требуется библиотека mutagen для чтения тегов и обложек:
    pip install mutagen --break-system-packages
    (на Windows/Mac обычно без флага:  pip install mutagen)

Опционально — Pillow, чтобы обложки отдавались уменьшенными превью (экономит канал
и ускоряет загрузку треков, особенно если в файлах большие встроенные обложки):
    pip install Pillow --break-system-packages

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
import uuid
import mimetypes
from urllib.parse import unquote, urlparse, parse_qs

try:
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

MUSIC_DIR = "music"
PORT = 8080
EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".weba", ".webm"}
FAVORITES_FILE = "favorites.json"
PLAYLISTS_FILE = "playlists.json"

# Секретный код доступа. Замените на свой — этот же код нужно будет ввести на сайте.
ACCESS_KEY = "7510"

# Можно сузить до адреса вашего сайта вместо "*" для дополнительной строгости,
# например: "https://ваш-логин.github.io"
ALLOWED_ORIGIN = "*"

# Избранное хранится в отдельном json-файле рядом со скриптом, чтобы переживало перезапуски.
_favorites = set()

# Плейлисты: {id: {"name": str, "tracks": [rel, ...]}}, тоже в отдельном файле.
_playlists = {}


def load_favorites():
    global _favorites
    if os.path.isfile(FAVORITES_FILE):
        try:
            with open(FAVORITES_FILE, "r", encoding="utf-8") as fh:
                _favorites = set(json.load(fh))
        except Exception:
            _favorites = set()


def save_favorites():
    try:
        with open(FAVORITES_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted(_favorites), fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_playlists():
    global _playlists
    if os.path.isfile(PLAYLISTS_FILE):
        try:
            with open(PLAYLISTS_FILE, "r", encoding="utf-8") as fh:
                _playlists = json.load(fh)
        except Exception:
            _playlists = {}


def save_playlists():
    try:
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(_playlists, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Кэш тегов: {rel_path: (mtime, size, meta_dict)} — чтобы не перечитывать теги
# у всех файлов при каждом обновлении библиотеки.
_tag_cache = {}


def parse_name_fallback(rel_path):
    """Если тегов нет — пробуем угадать исполнителя/название по имени файла."""
    filename = rel_path.split("/")[-1]
    base = os.path.splitext(filename)[0]
    m = re.match(r"^(.{1,60}?)\s[-–—]\s(.+)$", base)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, base


def read_meta(full_path, rel_path, ext):
    """Возвращает dict: title, artist, album, duration, trackNumber, hasArt."""
    fallback_artist, fallback_title = parse_name_fallback(rel_path)
    meta = {
        "title": fallback_title,
        "artist": fallback_artist,
        "album": None,
        "duration": None,
        "trackNumber": None,
        "hasArt": False,
    }
    if not HAVE_MUTAGEN:
        return meta
    try:
        f = mutagen.File(full_path, easy=True)
        if f is not None:
            if f.tags:
                def first(key):
                    v = f.tags.get(key)
                    return str(v[0]).strip() if v else None
                meta["title"] = first("title") or meta["title"]
                meta["artist"] = first("artist") or meta["artist"]
                meta["album"] = first("album")
                tn = first("tracknumber")
                if tn:
                    try:
                        meta["trackNumber"] = int(re.split(r"[/\\]", tn)[0])
                    except ValueError:
                        pass
            if hasattr(f.info, "length"):
                meta["duration"] = round(f.info.length, 1)
    except Exception:
        pass

    try:
        if ext == "mp3":
            tags = ID3(full_path)
            meta["hasArt"] = any(t.FrameID == "APIC" for t in tags.values())
        elif ext == "flac":
            meta["hasArt"] = bool(FLAC(full_path).pictures)
        elif ext in ("m4a", "mp4"):
            mp4 = MP4(full_path)
            meta["hasArt"] = bool(mp4.tags and mp4.tags.get("covr"))
    except Exception:
        pass

    return meta


try:
    from PIL import Image
    import io
    HAVE_PILLOW = True
except ImportError:
    HAVE_PILLOW = False

THUMB_MAX_SIDE = 320
_thumb_cache = {}  # rel -> (mtime, size, jpeg_bytes)


def get_cover_bytes(full_path, ext):
    try:
        if ext == "mp3":
            tags = ID3(full_path)
            for tag in tags.values():
                if tag.FrameID == "APIC":
                    return tag.data, tag.mime
        elif ext == "flac":
            f = FLAC(full_path)
            if f.pictures:
                p = f.pictures[0]
                return p.data, p.mime
        elif ext in ("m4a", "mp4"):
            f = MP4(full_path)
            covers = f.tags.get("covr") if f.tags else None
            if covers:
                c = covers[0]
                mime = "image/png" if c.imageformat == 14 else "image/jpeg"
                return bytes(c), mime
    except Exception:
        pass
    return None, None


def get_thumb_bytes(full_path, rel, ext):
    """Уменьшенная копия обложки (JPEG, до THUMB_MAX_SIDE px) — экономит канал
    при просмотре сетки исполнителей и списков треков. Кэшируется в памяти."""
    try:
        stat = os.stat(full_path)
    except OSError:
        return None, None
    cached = _thumb_cache.get(rel)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], "image/jpeg"

    data, mime = get_cover_bytes(full_path, ext)
    if not data:
        return None, None
    if not HAVE_PILLOW:
        return data, mime  # отдаём оригинал, если Pillow не установлен

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        thumb_bytes = buf.getvalue()
        _thumb_cache[rel] = (stat.st_mtime, stat.st_size, thumb_bytes)
        return thumb_bytes, "image/jpeg"
    except Exception:
        return data, mime  # если PIL не смог разобрать формат — отдаём оригинал


def scan_tracks():
    tracks = []
    for root, _dirs, files in os.walk(MUSIC_DIR):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower().lstrip(".")
            if "." + ext not in EXTS:
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, MUSIC_DIR).replace(os.sep, "/")
            try:
                stat = os.stat(full)
            except OSError:
                continue
            cached = _tag_cache.get(rel)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                meta = cached[2]
            else:
                meta = read_meta(full, rel, ext)
                _tag_cache[rel] = (stat.st_mtime, stat.st_size, meta)
            track = {"rel": rel, "ext": ext}
            track.update(meta)
            track["favorite"] = rel in _favorites
            track["playlistIds"] = [pid for pid, pl in _playlists.items() if rel in pl.get("tracks", [])]
            tracks.append(track)
    tracks.sort(key=lambda t: (
        (t["artist"] or "").lower(),
        (t["album"] or "").lower(),
        t["trackNumber"] if t["trackNumber"] is not None else 9999,
        (t["title"] or "").lower(),
    ))
    return tracks


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")

    def _check_key(self):
        query = parse_qs(urlparse(self.path).query)
        key = query.get("key", [None])[0]
        if key != ACCESS_KEY:
            body = "Unauthorized".encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._handle(send_body=True)

    def do_HEAD(self):
        self._handle(send_body=False)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/favorite":
            self.handle_favorite()
        elif path == "/playlists":
            self.handle_playlists_post()
        else:
            self.send_error(404, "Not found")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def handle_favorite(self):
        if not self._check_key():
            return
        data = self._read_json_body()
        rel = data.get("rel")
        want_favorite = bool(data.get("favorite"))

        ok = False
        if rel and self._resolve(rel):
            if want_favorite:
                _favorites.add(rel)
            else:
                _favorites.discard(rel)
            save_favorites()
            ok = True

        self._send_json({"ok": ok}, 200 if ok else 400)

    def handle_playlists_post(self):
        if not self._check_key():
            return
        data = self._read_json_body()
        action = data.get("action")
        ok = False
        new_id = None

        if action == "create":
            name = (data.get("name") or "").strip()[:120] or "Новый плейлист"
            new_id = uuid.uuid4().hex[:10]
            _playlists[new_id] = {"name": name, "tracks": []}
            ok = True
        elif action == "rename":
            pid = data.get("id")
            name = (data.get("name") or "").strip()[:120]
            if pid in _playlists and name:
                _playlists[pid]["name"] = name
                ok = True
        elif action == "delete":
            pid = data.get("id")
            if pid in _playlists:
                del _playlists[pid]
                ok = True
        elif action == "add_track":
            pid = data.get("id")
            rel = data.get("rel")
            if pid in _playlists and rel and self._resolve(rel):
                if rel not in _playlists[pid]["tracks"]:
                    _playlists[pid]["tracks"].append(rel)
                ok = True
        elif action == "remove_track":
            pid = data.get("id")
            rel = data.get("rel")
            if pid in _playlists and rel in _playlists[pid].get("tracks", []):
                _playlists[pid]["tracks"].remove(rel)
                ok = True

        if ok:
            save_playlists()
        self._send_json({"ok": ok, "id": new_id, "playlists": _playlists}, 200 if ok else 400)

    def _handle(self, send_body):
        path = self.path.split("?")[0]
        if path in ("/manifest.json", "/"):
            self.serve_manifest(send_body)
        elif path == "/playlists":
            self.serve_playlists(send_body)
        elif path.startswith("/music/"):
            self.serve_file(path[len("/music/"):], send_body)
        elif path.startswith("/art/"):
            self.serve_art(path[len("/art/"):], send_body)
        else:
            self.send_error(404, "Not found")

    def serve_playlists(self, send_body=True):
        if not self._check_key():
            return
        body = json.dumps(_playlists, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def serve_manifest(self, send_body=True):
        if not self._check_key():
            return
        tracks = scan_tracks() if os.path.isdir(MUSIC_DIR) else []
        body = json.dumps(tracks, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _resolve(self, rel):
        rel = unquote(rel)
        full = os.path.normpath(os.path.join(MUSIC_DIR, rel))
        base = os.path.normpath(MUSIC_DIR)
        if not full.startswith(base) or not os.path.isfile(full):
            return None
        return full

    def serve_art(self, rel, send_body=True):
        if not self._check_key():
            return
        full = self._resolve(rel)
        if not full or not HAVE_MUTAGEN:
            self.send_error(404, "No cover")
            return
        ext = os.path.splitext(full)[1].lower().lstrip(".")
        query = parse_qs(urlparse(self.path).query)
        size = query.get("size", ["thumb"])[0]
        if size == "full":
            data, mime = get_cover_bytes(full, ext)
        else:
            data, mime = get_thumb_bytes(full, rel, ext)
        if not data:
            self.send_error(404, "No cover")
            return
        self.send_response(200)
        self.send_header("Content-Type", mime or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self._cors()
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def serve_file(self, rel, send_body=True):
        if not self._check_key():
            return
        full = self._resolve(rel)
        if not full:
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

    load_favorites()
    load_playlists()

    if not HAVE_MUTAGEN:
        print("ВНИМАНИЕ: библиотека mutagen не установлена — теги и обложки читаться не будут,")
        print("сайт покажет только имена файлов. Установите:  pip install mutagen --break-system-packages")
        print()

    if HAVE_MUTAGEN and not HAVE_PILLOW:
        print("Совет: установите Pillow, чтобы обложки сжимались в превью и не грузили канал:")
        print("   pip install Pillow --break-system-packages")
        print("Без неё обложки будут отдаваться в оригинальном размере — это и есть частая")
        print("причина медленной загрузки треков, если в файлах большие встроенные обложки.")
        print()

    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Сервер музыки запущен: http://127.0.0.1:{PORT}")
        print("Список треков:  http://127.0.0.1:%d/manifest.json?key=..." % PORT)
        print("Не закрывайте это окно, пока хотите слушать музыку через сайт.")
        httpd.serve_forever()
