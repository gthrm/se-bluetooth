#!/usr/bin/env python3
"""SE Bluetooth: a window for transferring files to/from a Sony Ericsson W810i over Bluetooth.

Starts a local server on 127.0.0.1 and opens the UI in the browser. Bluetooth runs on
the main thread (IOBluetooth needs its run loop); HTTP requests are served on other
threads and hand their phone work to it.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import io
import json
import mimetypes
import os
import queue
import signal
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt  # noqa: E402
import media  # noqa: E402
import obex  # noqa: E402

PORT = 8766
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "se_ui.html")
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
MAX_UPLOAD = 200 * 1024 * 1024  # source files may be big; they are checked again after compression
CONFIG = os.path.expanduser("~/Library/Application Support/SE Bluetooth/phone.json")
LOG = os.path.expanduser("~/Library/Logs/SE Bluetooth.log")
LOST = "The phone stopped responding. Check that Bluetooth is on and the phone is close, then click Refresh."
RETRY_AFTER = 10  # seconds before trying again after a failed connect
IDLE_DISCONNECT = 300  # seconds without phone work before the Bluetooth link is closed
THUMB_READ = 16 * 1024  # camera photos keep their EXIF thumbnail in the first few KB
SMALL_PICTURE = 48 * 1024  # pictures up to this size are previewed whole

mimetypes.add_type("audio/amr", ".amr")
mimetypes.add_type("audio/midi", ".mid")
mimetypes.add_type("video/3gpp", ".3gp")

# where each kind of file goes on each memory (the phone's own media folders)
FOLDERS = {
    "/Phone memory": {"picture": "Pictures", "music": "Sounds", "video": "Videos", "theme": "Themes"},
    "/Memory Stick": {"picture": "MSSEMC/Media files/image", "music": "MSSEMC/Media files/audio",
                      "video": "MSSEMC/Media files/video", "theme": "MSSEMC/Media files/theme"},
}

jobs = {}  # job id -> {"stage", "done", "total", "rate", "eta"} for the progress display
state = {"phone": None, "last_used": 0.0, "paired": [], "last_ping": time.time()}
listings = {}  # folder path -> [entry], cleared on Refresh and after changes
cache = OrderedDict()  # file path -> {"size": n, "raw": bytes, "thumb": bytes, ...}
cache_lock = threading.Lock()
cache_total = [0]
path_locks = {}
CACHE_BYTES = 24 * 1024 * 1024


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


class AppError(Exception):
    def __init__(self, msg, code=500, obex_code=None):
        super().__init__(msg)
        self.code = code
        self.obex_code = obex_code  # the phone's OBEX response code, when it refused a request


# --- the Bluetooth thread -------------------------------------------------------

class Worker:
    """Runs phone work on the main thread, one job at a time."""

    def __init__(self):
        self.q = queue.Queue()
        self.busy = False
        self.stop = threading.Event()

    def call(self, fn):
        if self.stop.is_set():
            raise AppError("The app is quitting", 503)
        done, box = threading.Event(), {}
        self.q.put((fn, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def run_forever(self):
        last_scan = 0.0
        while not self.stop.is_set():
            try:
                fn, done, box = self.q.get_nowait()
            except queue.Empty:
                bt.pump(0.1)
                now = time.time()
                if state["phone"] is not None and now - state["last_used"] > IDLE_DISCONNECT:
                    drop_phone()
                if now - last_scan > 5:
                    state["paired"] = bt.paired_phones()
                    last_scan = now
                continue
            self.busy = True
            try:
                box["result"] = fn()
            except Exception as e:  # noqa: BLE001 - handed back to the waiting request
                box["error"] = e
            finally:
                self.busy = False
                state["last_used"] = time.time()
                done.set()
        drop_phone()
        while not self.q.empty():  # nobody may wait forever on a job we won't run
            _, done, box = self.q.get_nowait()
            box["error"] = AppError("The app is quitting", 503)
            done.set()


worker = Worker()


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f)


def chosen_phone():
    """The phone to talk to: the one picked in the app, else the first paired Sony Ericsson."""
    paired = state["paired"]
    addr = load_config().get("address")
    for p in paired:
        if p["address"] == addr:
            return p
    return bt.preferred_phone(paired)


def drop_phone():
    """Close the OBEX session and the Bluetooth link (worker thread only)."""
    ph, state["phone"] = state["phone"], None
    if ph is not None:
        log("disconnect")
        try:
            ph.close()
        except Exception:  # noqa: BLE001 - the link may already be gone
            pass


def with_phone(fn, retry=False):
    """Run fn(phone) on the Bluetooth thread, connecting first if needed.

    The W810i sometimes stops answering mid-session and is fine on a new connection,
    so with retry=True (for reads only) a lost link is reopened and fn run once more.
    """
    def job():
        try:
            return attempt()
        except obex.ProtocolLost:
            if not retry or worker.stop.is_set():
                raise
            log("retrying on a new connection")
            return attempt()

    def attempt():
        if state["phone"] is None:
            phone = chosen_phone()
            if phone is None:
                raise AppError("No phone paired yet. Click “Find phone”.", 409)
            failed = state.get("connect_failed")
            if failed and time.time() - failed[0] < RETRY_AFTER:
                raise failed[1]  # just failed: don't make every waiting request wait again
            log("connecting to %s" % phone["address"])
            start = time.time()
            try:
                state["phone"] = obex.Phone(phone["address"])
            except obex.SEError as e:
                state["connect_failed"] = (time.time(), e)
                raise
            state["connect_failed"] = None
            log("connected in %.1f s" % (time.time() - start))
        try:
            return fn(state["phone"])
        except obex.ProtocolLost as e:
            log("lost: %s" % e)
            drop_phone()
            raise

    try:
        return worker.call(job)
    except obex.ProtocolLost:
        raise AppError(LOST, 503)
    except obex.OBEXError as e:
        raise AppError(str(e), 404 if e.code == 0xC4 else 502, obex_code=e.code)
    except obex.SEError as e:
        log("error: %s" % e)
        raise AppError(str(e), 503)


# --- folders and files ----------------------------------------------------------

def norm(path):
    return "/" + "/".join(obex.split(path))


def join(folder, name):
    return norm(folder).rstrip("/") + "/" + name


def volume(path):
    parts = obex.split(path)
    return "/" + parts[0] if parts else "/"


def listdir(path, refresh=False):
    path = norm(path)
    if refresh and path == "/":
        state["connect_failed"] = None  # the Refresh button: try the phone again now
    with cache_lock:
        if not refresh and path in listings:
            return listings[path]
    entries = with_phone(lambda ph: ph.listdir(path), retry=True)
    for e in entries:
        e["path"] = join(path, e["name"])
    with cache_lock:
        listings[path] = entries
    return entries


def forget_listing(folder):
    with cache_lock:
        listings.pop(norm(folder), None)


def file_size(path):
    folder, name = norm(path).rsplit("/", 1)
    for e in listdir(folder or "/"):
        if e["name"] == name and not e["folder"]:
            return e["size"]
    raise AppError("No such file on the phone: " + path, 404)


def _entry_bytes(entry):
    return sum(len(v) for k, v in entry.items() if k != "size")


def _drop(path):
    entry = cache.pop(path, None)
    if entry:
        cache_total[0] -= _entry_bytes(entry)


def _cached(path, size, key):
    with cache_lock:
        entry = cache.get(path)
        if entry and entry["size"] != size:
            _drop(path)  # the file changed on the phone
            return None
        if entry and key in entry:
            cache.move_to_end(path)
            return entry[key]
        return None


def _remember(path, size, key, data):
    with cache_lock:
        entry = cache.setdefault(path, {"size": size})
        cache_total[0] += len(data) - len(entry.get(key, b""))
        entry[key] = data
        cache.move_to_end(path)
        while len(cache) > 1 and cache_total[0] > CACHE_BYTES:
            _drop(next(iter(cache)))


def forget(path):
    with cache_lock:
        _drop(path)


def _path_lock(path):
    with cache_lock:
        return path_locks.setdefault(path, threading.RLock())


def job_update(job, **kw):
    if job:
        jobs.setdefault(job, {}).update(kw)


def _transfer_progress(job, base=0, grand_total=None):
    """on_progress(done, total) for a phone read/write, with speed and time left."""
    start = time.time()

    def progress(done, total):
        rate = done / max(time.time() - start, 0.001)
        total_all = grand_total or total
        job_update(job, done=base + done, total=total_all, rate=rate,
                   eta=(total_all - base - done) / rate if rate else None)
    return progress if job else None


def read_file(path, job=None, base=0, grand_total=None):
    path = norm(path)
    size = file_size(path)
    with _path_lock(path):
        raw = _cached(path, size, "raw")
        if raw is None:
            job_update(job, stage="reading", done=base, total=grand_total or size)
            raw = with_phone(lambda ph: ph.read(path, _transfer_progress(job, base, grand_total)), retry=True)
            _remember(path, size, "raw", raw)
        return raw


def thumbnail(path):
    """A small JPEG for a picture: the camera's EXIF thumbnail, or the whole file if it's small."""
    path = norm(path)
    size = file_size(path)
    data = _cached(path, size, "thumb")
    if data is not None:
        return data
    with _path_lock(path):
        raw = _cached(path, size, "raw")
        if raw is None and size <= SMALL_PICTURE:
            raw = read_file(path)
        if raw is not None:
            data = media.exif_thumbnail(raw) or raw
        else:
            head = with_phone(lambda ph: ph.read(path, limit=THUMB_READ), retry=True)
            data = media.exif_thumbnail(head)
            if data is None:
                raise AppError("No thumbnail in %s" % path, 415)
        _remember(path, size, "thumb", data)
        return data


def preview(path, kind):
    """kind: 'poster' (JPEG frame) or 'video' (browser-playable MP4) for a phone video."""
    path = norm(path)
    size = file_size(path)
    with _path_lock(path):
        data = _cached(path, size, kind)
        if data is not None:
            return data
        raw = read_file(path)
        try:
            data = media.video_preview(raw, path, poster=(kind == "poster"))
        except media.MediaError as e:
            raise AppError(str(e), 415)
        _remember(path, size, kind, data)
        return data


def unique_name(folder, name):
    taken = {e["name"].lower() for e in listdir(folder)}
    base, ext = os.path.splitext(name)
    candidate, i = name, 1
    while candidate.lower() in taken:
        candidate = "%s_%d%s" % (base, i, ext)
        i += 1
    return candidate


def sorted_folder(folder, name):
    """The phone's media folder for this kind of file on the open folder's memory, if it exists."""
    vol = volume(folder)
    sub = FOLDERS.get(vol, {}).get(media.kind(name))
    if not sub:
        return folder
    target = join(vol, sub)
    try:
        listdir(target)
    except AppError as e:
        if e.code == 503:
            raise
        return folder  # this memory has no such folder: keep the open one
    return target


def write_file(folder, name, data, image_mode=None, mp3_kbps=None, video_quality=None, sort=False, job=None):
    folder = norm(folder)
    if folder == "/":
        raise AppError("Open “Phone memory” or “Memory Stick” first", 400)
    original = len(data)
    job_update(job, stage="compressing", done=0, total=0)
    try:
        on_progress = (lambda f: job_update(job, done=int(f * 1000), total=1000)) if job else None
        data, name = media.prepare(data, name, image_mode=image_mode, mp3_kbps=mp3_kbps,
                                   video_quality=video_quality, on_progress=on_progress)
    except media.MediaError as e:
        raise AppError(str(e), 422)
    job_update(job, stage="waiting", done=0, total=len(data))
    if sort:  # by what is uploaded (after conversion): a kept .mkv isn't a phone video
        folder = sorted_folder(folder, name)
    path = join(folder, unique_name(folder, obex.safe_name(name)))

    def go(ph):
        job_update(job, stage="writing", done=0)
        ph.write(path, data, _transfer_progress(job))

    try:
        with_phone(go)
    except AppError as e:
        if e.obex_code in obex.NO_SPACE:
            raise AppError("Not enough space on the phone for %s (%s)" % (name, obex.human(len(data))), 507)
        raise
    finally:
        forget_listing(folder)
        forget(path)
    return {"path": path, "original": original, "size": len(data)}


def delete_paths(paths):
    def go(ph):
        for p in paths:
            ph.delete(norm(p))
            forget(norm(p))

    try:
        with_phone(go)
    finally:
        for p in paths:
            forget_listing(norm(p).rsplit("/", 1)[0] or "/")
            forget_listing(p)


def make_folder(path):
    path = norm(path)
    if len(obex.split(path)) < 2:
        raise AppError("Open “Phone memory” or “Memory Stick” first", 400)
    folder, name = path.rsplit("/", 1)
    path = join(folder, obex.safe_name(name))
    try:
        with_phone(lambda ph: ph.mkdir(path))
    finally:
        forget_listing(folder)
    return path


def zip_files(paths, job=None):
    sizes = [file_size(p) for p in paths]
    total, done = sum(sizes), 0
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for p, size in zip(paths, sizes):
            name = os.path.basename(norm(p))
            base, ext = os.path.splitext(name)
            i = 1
            while name.lower() in used:
                name = "%s_%d%s" % (base, i, ext)
                i += 1
            used.add(name.lower())
            z.writestr(name, read_file(p, job, done, total))
            done += size
    return buf.getvalue()


def scan():
    return worker.call(lambda: bt.scan(8))


def pair(address):
    def go():
        drop_phone()
        bt.pair(address)
        state["paired"] = bt.paired_phones()

    try:
        worker.call(go)
    except bt.BTError as e:
        raise AppError(str(e), 502)
    cfg = load_config()
    cfg["address"] = bt._norm(address)
    save_config(cfg)
    # another phone may have files at the same paths: forget everything about the old one
    state["connect_failed"] = None
    with cache_lock:
        listings.clear()
        cache.clear()
        cache_total[0] = 0


def status():
    phone = chosen_phone()
    return {"phone": phone and {"name": phone["name"], "address": phone["address"]},
            "connected": state["phone"] is not None, "busy": worker.busy}


# --- HTTP -------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, filename=None, inline=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            disp = "inline" if inline else "attachment"
            self.send_header("Content-Disposition",
                             "%s; filename*=UTF-8''%s" % (disp, urllib.parse.quote(filename)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise AppError("File is too large for the phone", 413)
        self.body_read = True
        return self.rfile.read(n) if n else b""

    def _drain(self):
        """Read and drop an unread request body, so the browser gets our error reply
        instead of a reset connection while it is still sending."""
        if self.body_read:
            return
        self.body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        while n > 0:
            chunk = self.rfile.read(min(n, 1 << 20))
            if not chunk:
                break
            n -= len(chunk)

    def _trusted(self, post):
        # Only our own page may talk to us: blocks DNS rebinding (Host) and
        # cross-site requests (custom header forces a CORS preflight we never allow).
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return False
        if not post:
            return True
        origin = self.headers.get("Origin")
        if origin is not None and origin.split("//", 1)[-1] not in ALLOWED_HOSTS:
            return False
        return self.headers.get("X-SE") == "1"

    def _handle(self, post):
        self.body_read = False
        if not self._trusted(post):
            self.send_error(403)
            return
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            route = (self.command, u.path)
            if route == ("GET", "/"):
                with open(HTML, "rb") as f:
                    self._bytes(f.read(), "text/html; charset=utf-8")
            elif route == ("GET", "/api/status"):
                self._json(status())
            elif route == ("GET", "/api/ping"):
                state["last_ping"] = time.time()
                self._json({"ok": True})
            elif route == ("GET", "/api/list"):
                path = q.get("path", "/")
                self._json({"path": norm(path), "entries": listdir(path, refresh=q.get("refresh") == "1")})
            elif route == ("GET", "/api/caps"):
                self._json({"images": media.images_supported(), "mp3": media.find_ffmpeg() is not None,
                            "video": media.video_supported(), "video_preview": media.video_preview_supported(),
                            "screen": media.SCREEN, "bitrates": media.MP3_BITRATES})
            elif route == ("GET", "/api/progress"):
                self._json(jobs.get(q.get("job"), {}))
            elif route == ("GET", "/api/thumb"):
                data = thumbnail(q["path"])
                self._bytes(data, media.sniff_image(data) or "image/jpeg")
            elif route == ("GET", "/api/file"):
                path = q["path"]
                job = q.get("job")
                try:
                    if q.get("preview") in ("poster", "video"):
                        data = preview(path, q["preview"])
                        ctype = "image/jpeg" if q["preview"] == "poster" else "video/mp4"
                    else:
                        data = read_file(path, job)
                        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                        if q.get("preview") == "1":
                            ctype = media.sniff_image(data) or ctype
                finally:
                    jobs.pop(job, None)
                self._bytes(data, ctype, os.path.basename(norm(path)), inline=q.get("inline") == "1")
            elif route == ("POST", "/api/upload"):
                img = q.get("img")
                kbps = int(q["mp3"]) if q.get("mp3") else None
                video = q.get("video") or None
                if img and img not in media.IMAGE_MODES or kbps and kbps not in media.MP3_BITRATES \
                        or video and video not in media.VIDEO_QUALITY:
                    raise AppError("Unknown conversion option", 400)
                job = q.get("job")
                try:
                    self._json(write_file(q["dir"], q["name"], self._body(), img, kbps, video,
                                          sort=q.get("sort") == "1", job=job))
                finally:
                    jobs.pop(job, None)
            elif route == ("POST", "/api/delete"):
                delete_paths(json.loads(self._body())["paths"])
                self._json({"ok": True})
            elif route == ("POST", "/api/mkdir"):
                self._json({"path": make_folder(json.loads(self._body())["path"])})
            elif route == ("POST", "/api/zip"):
                job = q.get("job")
                try:
                    data = zip_files(json.loads(self._body())["paths"], job)
                finally:
                    jobs.pop(job, None)
                self._bytes(data, "application/zip", "W810i_files.zip")
            elif route == ("POST", "/api/scan"):
                self._json({"phones": scan()})
            elif route == ("POST", "/api/pair"):
                pair(json.loads(self._body())["address"])
                self._json(status())
            elif route == ("POST", "/api/disconnect"):
                worker.call(drop_phone)
                self._json(status())
            elif route == ("POST", "/api/quit"):
                self._json({"ok": True})
                threading.Thread(target=shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except AppError as e:
            self._fail(str(e), e.code)
        except bt.BTError as e:
            self._fail(str(e), 502)
        except (KeyError, ValueError) as e:
            self._fail("Bad request: %s" % e, 400)
        except Exception as e:  # noqa: BLE001 - always answer, never drop the connection
            log("unexpected: %r" % e)
            self._fail("Unexpected error: %s" % e, 500)

    def _fail(self, msg, code):
        if self.command == "POST":
            self._drain()
        self._json({"error": msg}, code)

    def do_GET(self):
        self._handle(post=False)

    def do_POST(self):
        self._handle(post=True)


server = None


def quit_soon():
    worker.stop.set()
    bt.cancel.set()


def shutdown():
    time.sleep(0.3)
    quit_soon()
    server.shutdown()


def watchdog():
    # Stop once the browser tab has been gone for 10 minutes.
    # A big gap between our own ticks means the Mac was asleep: don't count that time.
    last_tick = time.time()
    while True:
        time.sleep(15)
        now = time.time()
        if now - last_tick > 60:
            state["last_ping"] = now
        last_tick = now
        if now - state["last_ping"] > 600 and not worker.busy:
            shutdown()
            return


def main():
    global server
    url = "http://127.0.0.1:%d/" % PORT
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(url)  # already running: just open the window
        return
    server.daemon_threads = True
    # quit cleanly on Ctrl-C / kill, so the phone isn't left with a half-open session
    signal.signal(signal.SIGTERM, lambda *a: quit_soon())
    signal.signal(signal.SIGINT, lambda *a: quit_soon())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    state["paired"] = bt.paired_phones()
    webbrowser.open(url)
    worker.run_forever()
    server.shutdown()


if __name__ == "__main__":
    main()
