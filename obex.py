#!/usr/bin/env python3
"""Transfer files to/from a Sony Ericsson phone (W810i etc.) over Bluetooth (OBEX File Transfer).

  ./se scan                     search for phones nearby (make the phone visible first)
  ./se pair <address> [code]    pair with a phone (enter the same code on the phone; default 0000)
  ./se ls [folder]              list a folder, e.g. "/Phone memory/Pictures"
  ./se tree [folder]            list every file below a folder
  ./se get <path> [dest]        download a file
  ./se pull <folder> <dir>      download every file in a folder
  ./se put <file> <path>        upload a file (path ending in / keeps the file name)
        [--fit | --wallpaper]     resize pictures to the 176x220 screen (wallpaper = crop)
        [--mp3 KBPS]              re-encode music as MP3 (128, 96, 64, 48 or 32 kbps)
        [--video small|normal]    convert video to 3GP 176x144
  ./se rm <path>                delete a file or an empty folder
  ./se mkdir <path>             create a folder

The phone is the first paired phone, or set SE_PHONE=<address>.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import html
import os
import re
import sys

import bt

# OBEX File Transfer target (Folder Browsing service UUID)
FTP_TARGET = bytes.fromhex("F9EC7BC4953C11D2984E525400DC9E09")
OUR_MAX_PACKET = 0x2000

CONNECT, DISCONNECT, PUT, GET, SETPATH, ABORT = 0x80, 0x81, 0x02, 0x03, 0x85, 0xFF
FINAL = 0x80
CONTINUE, SUCCESS = 0x90, 0xA0

H_NAME, H_TYPE, H_LENGTH, H_BODY, H_END_BODY, H_WHO, H_CONN_ID, H_TARGET = \
    0x01, 0x42, 0xC3, 0x48, 0x49, 0x4A, 0xCB, 0x46

# OBEX response codes mirror HTTP status codes (0xC4 = 404, 0xCD = 413, ...)
RESPONSES = {
    0xC0: "bad request", 0xC1: "not allowed", 0xC3: "access denied", 0xC4: "not found",
    0xC5: "not allowed", 0xC6: "not acceptable", 0xC9: "conflict", 0xCB: "length required",
    0xCC: "precondition failed", 0xCD: "file too large or no space", 0xCF: "unsupported type",
    0xD0: "phone error", 0xD1: "not supported", 0xD3: "phone busy", 0xE0: "memory full",
}
NO_SPACE = (0xCD, 0xE0)


class SEError(Exception):
    pass


class ProtocolLost(SEError):
    """The Bluetooth link dropped or the phone stopped answering."""


class OBEXError(SEError):
    def __init__(self, what, code):
        reason = RESPONSES.get(code, "error 0x%02x" % code)
        super().__init__("%s: %s" % (what, reason))
        self.code = code


def _header(hi, value):
    kind = hi & 0xC0
    if kind == 0x00:  # Unicode text, null-terminated UTF-16BE
        data = (value + "\0").encode("utf-16-be") if value else b""
        return bytes([hi]) + (3 + len(data)).to_bytes(2, "big") + data
    if kind == 0x40:  # byte sequence
        return bytes([hi]) + (3 + len(value)).to_bytes(2, "big") + value
    if kind == 0x80:
        return bytes([hi, value])
    return bytes([hi]) + value.to_bytes(4, "big")


def _parse_headers(data):
    out = {}
    i = 0
    while i < len(data):
        hi = data[i]
        kind = hi & 0xC0
        if kind in (0x00, 0x40):
            n = int.from_bytes(data[i + 1:i + 3], "big")
            if n < 3:
                raise ProtocolLost("Garbled reply from the phone")
            val = data[i + 3:i + n]
            if kind == 0x00:
                val = val.decode("utf-16-be", errors="replace").rstrip("\0")
            i += n
        elif kind == 0x80:
            val = data[i + 1]
            i += 2
        else:
            val = int.from_bytes(data[i + 1:i + 5], "big")
            i += 5
        if hi in (H_BODY, H_END_BODY):
            out[hi] = out.get(hi, b"") + val
        else:
            out[hi] = val
    return out


class Session:
    """An OBEX File Transfer session over a byte channel (write/read)."""

    def __init__(self, channel):
        self.ch = channel
        self.conn_id = None
        self.max_packet = 255
        code, extra, hdrs = self._request(CONNECT, [(H_TARGET, FTP_TARGET)],
                                          extra=bytes([0x10, 0]) + OUR_MAX_PACKET.to_bytes(2, "big"),
                                          connect=True, timeout=20)
        if code != SUCCESS:
            raise OBEXError("The phone refused file access", code)
        self.max_packet = min(OUR_MAX_PACKET, max(int.from_bytes(extra[2:4], "big"), 255))
        self.conn_id = hdrs.get(H_CONN_ID)

    # --- packets -----------------------------------------------------------
    def _request(self, opcode, headers=(), extra=b"", connect=False, timeout=15):
        hdrs = b""
        if self.conn_id is not None:
            hdrs += _header(H_CONN_ID, self.conn_id)
        for hi, val in headers:
            hdrs += _header(hi, val)
        pkt = bytes([opcode]) + (3 + len(extra) + len(hdrs)).to_bytes(2, "big") + extra + hdrs
        try:
            self.ch.write(pkt)
            head = self.ch.read(3, timeout)
            n = int.from_bytes(head[1:3], "big")
            if n < 3:
                raise ProtocolLost("Garbled reply from the phone")
            body = self.ch.read(n - 3, timeout) if n > 3 else b""
        except bt.ChannelClosed as e:
            raise ProtocolLost(str(e))
        rextra = b""
        if connect:
            rextra, body = body[:4], body[4:]
        return head[0], rextra, _parse_headers(body)

    def _body_room(self):
        # packet header + connection id + body header
        return self.max_packet - 3 - 5 - 3

    # --- operations ----------------------------------------------------------
    def setpath(self, name=None, up=False, create=False):
        """name=None and up=False goes to the root folder."""
        flags = (0x01 if up else 0) | (0 if create else 0x02)
        headers = [(H_NAME, name)] if name else ([] if up else [(H_NAME, "")])
        code, _, _ = self._request(SETPATH, headers, extra=bytes([flags, 0]))
        if code != SUCCESS:
            raise OBEXError("Can't open folder %s" % (name or ("..'" if up else "/")), code)

    def get(self, name=None, type_=None, on_progress=None, limit=None):
        """Download an object. With limit, stop (ABORT) once that many bytes arrived."""
        headers = []
        if name is not None:
            headers.append((H_NAME, name))
        if type_:
            headers.append((H_TYPE, type_.encode() + b"\0"))
        data = bytearray()
        total = None
        op = GET | FINAL
        while True:
            code, _, hdrs = self._request(op, headers)
            headers = []
            total = hdrs.get(H_LENGTH, total)
            data += hdrs.get(H_BODY, b"") + hdrs.get(H_END_BODY, b"")
            if on_progress:
                on_progress(len(data), total or len(data))
            if code == SUCCESS:
                return bytes(data)
            if code != CONTINUE:
                raise OBEXError("Can't read %s" % (name or type_), code)
            if limit is not None and len(data) >= limit:
                self.abort()
                return bytes(data)

    def put(self, name, data, on_progress=None):
        headers = [(H_NAME, name), (H_LENGTH, len(data))]
        # the first packet also carries the name and length
        room = max(self._body_room() - len(_header(H_NAME, name)) - 5, 0)
        sent = 0
        while True:
            chunk = data[sent:sent + room]
            last = sent + len(chunk) >= len(data)
            headers.append((H_END_BODY if last else H_BODY, chunk))
            code, _, _ = self._request(PUT | (FINAL if last else 0), headers, timeout=60)
            sent += len(chunk)
            if on_progress:
                on_progress(sent, len(data))
            if code != (SUCCESS if last else CONTINUE):
                raise OBEXError("Can't write %s" % name, code)
            if last:
                return
            headers = []
            room = self._body_room()

    def delete(self, name):
        code, _, _ = self._request(PUT | FINAL, [(H_NAME, name)])
        if code != SUCCESS:
            raise OBEXError("Can't delete %s" % name, code)

    def abort(self):
        self._request(ABORT)  # any answer ends the transfer; a lost link raises ProtocolLost

    def disconnect(self):
        try:
            self._request(DISCONNECT, timeout=3)
        except SEError:
            pass


def parse_listing(xml):
    """OBEX folder listing -> [{'name', 'folder', 'size', 'modified'}]."""
    text = xml.decode("utf-8", errors="replace")
    out = []
    for m in re.finditer(r"<(folder|file)\b([^>]*?)/?>", text):
        attrs = {k: html.unescape(v) for k, v in re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', m.group(2))}
        if "name" not in attrs:
            continue
        size = attrs.get("size", "")
        out.append({"name": attrs["name"], "folder": m.group(1) == "folder",
                    "size": int(size) if size.isdigit() else 0, "modified": attrs.get("modified", "")})
    return out



def split(path):
    return [p for p in path.split("/") if p]


class Phone:
    """File access by path ('/Phone memory/Pictures/x.jpg') on top of an OBEX session."""

    def __init__(self, address=None):
        address = address or os.environ.get("SE_PHONE") or default_address()
        try:
            self.channel = bt.Channel(address)
        except bt.ChannelClosed as e:
            raise ProtocolLost(str(e))
        except bt.BTError as e:
            raise SEError(str(e))
        try:
            self.session = Session(self.channel)
        except Exception:
            self.channel.close()
            raise
        self.name = self.channel.name
        self.address = address
        self.cwd = None  # unknown until the first cd

    def cd(self, path):
        parts = split(path)
        if self.cwd == parts:
            return
        s = self.session
        if self.cwd is not None and parts[:len(self.cwd)] == self.cwd:
            todo = parts[len(self.cwd):]
        elif self.cwd is not None and self.cwd[:len(parts)] == parts and len(self.cwd) - len(parts) <= 2:
            for _ in range(len(self.cwd) - len(parts)):
                self.cwd = None
                s.setpath(up=True)
            self.cwd = parts
            return
        else:
            self.cwd = None
            s.setpath()
            todo = parts
            self.cwd = []
        for p in todo:
            try:
                s.setpath(p)
            except OBEXError:
                self.cwd = None  # we may be half way down: don't trust it
                raise
            self.cwd.append(p)

    def _parent(self, path):
        parts = split(path)
        if not parts:
            raise SEError("Not a file: " + path)
        self.cd("/".join(parts[:-1]))
        return parts[-1]

    def listdir(self, path="/"):
        self.cd(path)
        return parse_listing(self.session.get(type_="x-obex/folder-listing"))

    def walk(self, path="/"):
        """Every file below path: [(path, size)]."""
        out = []
        base = "/" + "/".join(split(path))
        for e in self.listdir(path):
            p = base.rstrip("/") + "/" + e["name"]
            if e["folder"]:
                out += self.walk(p)
            else:
                out.append((p, e["size"]))
        return out

    def read(self, path, on_progress=None, limit=None):
        name = self._parent(path)
        try:
            return self.session.get(name, on_progress=on_progress, limit=limit)
        except ProtocolLost:
            self.cwd = None
            raise

    def write(self, path, data, on_progress=None):
        name = self._parent(path)
        self.session.put(name, data, on_progress)

    def delete(self, path):
        name = self._parent(path)
        self.session.delete(name)

    def mkdir(self, path):
        name = self._parent(path)
        self.session.setpath(name, create=True)
        self.cwd.append(name)

    def close(self):
        try:
            self.session.disconnect()
        finally:
            self.channel.close()


def default_address():
    phone = bt.preferred_phone(bt.paired_phones())
    if phone is None:
        raise SEError("No paired phone. Run: ./se scan, then ./se pair <address>")
    return phone["address"]


ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name):
    """Drop characters a FAT memory card can't store; keep the length sane."""
    base, ext = os.path.splitext(name)
    base = ILLEGAL.sub("_", base).strip(" .")[:60] or "file"
    return base + ILLEGAL.sub("_", ext)[:8]


def progress(done, total):
    w = 30
    k = int(w * done / total) if total else w
    sys.stdout.write("\r  [%s%s] %d/%d" % ("#" * k, "." * (w - k), done, total))
    sys.stdout.flush()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024


def put_args(args):
    """put [--fit | --wallpaper] [--mp3 KBPS] [--video small|normal] <file> <path>"""
    import media
    opts, rest = {}, []
    it = iter(args)
    for a in it:
        if a == "--fit":
            opts["image_mode"] = "fit"
        elif a == "--wallpaper":
            opts["image_mode"] = "fill"
        elif a == "--mp3":
            kbps = next(it, "")
            kbps = int(kbps) if kbps.isdigit() else 0
            if kbps not in media.MP3_BITRATES:
                raise SEError("--mp3 must be one of %s" % ", ".join(map(str, media.MP3_BITRATES)))
            opts["mp3_kbps"] = kbps
        elif a == "--video":
            q = next(it, "")
            if q not in media.VIDEO_QUALITY:
                raise SEError("--video must be one of %s" % ", ".join(media.VIDEO_QUALITY))
            opts["video_quality"] = q
        else:
            rest.append(a)
    if len(rest) != 2:
        raise SEError("usage: put [--fit | --wallpaper] [--mp3 KBPS] [--video Q] <file> <path>")
    return rest[0], rest[1], opts


USAGE = {"get": 1, "pull": 2, "put": 2, "rm": 1, "mkdir": 1}  # required arguments


def main(argv):
    if len(argv) < 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    op = argv[0]
    if op == "scan":
        print("Searching for 10 seconds (make the phone visible: Settings > Connectivity > Bluetooth > Visibility)...")
        for p in bt.scan():
            print("%s  %s%s" % (p["address"], p["name"], "  (paired)" if p["paired"] else ""))
        return 0
    if op == "pair":
        if len(argv) < 2:
            raise SEError("usage: pair <address> [code]")
        code = argv[2] if len(argv) > 2 else "0000"
        print("Pairing... when the phone asks for a passcode, enter %s" % code)
        try:
            bt.pair(argv[1], code)
        except bt.BTError as e:
            raise SEError(str(e))
        print("Paired.")
        return 0
    if len(argv) - 1 < USAGE.get(op, 0):
        raise SEError("Not enough arguments for %s. See ./se --help" % op)
    ph = Phone()
    try:
        return run(ph, op, argv[1:])
    finally:
        ph.close()


def run(ph, op, args):
    if op == "ls":
        for e in ph.listdir(args[0] if args else "/"):
            print("%10s  %s" % ("<dir>" if e["folder"] else e["size"], e["name"]))
    elif op == "tree":
        for p, size in ph.walk(args[0] if args else "/"):
            print("%10d  %s" % (size, p))
    elif op == "get":
        src = args[0]
        dest = args[1] if len(args) > 1 else os.path.basename(src)
        if os.path.isdir(dest):
            dest = os.path.join(dest, os.path.basename(src))
        print(src, "->", dest)
        data = ph.read(src, progress)
        with open(dest, "wb") as f:
            f.write(data)
        print()
    elif op == "pull":
        folder, outdir = args[0], args[1]
        os.makedirs(outdir, exist_ok=True)
        for e in ph.listdir(folder):
            if e["folder"]:
                continue
            src = folder.rstrip("/") + "/" + e["name"]
            # the name comes from the phone: never let it point outside outdir
            dest = os.path.join(outdir, safe_name(e["name"]))
            print(src, "->", dest)
            data = ph.read(src, progress)
            with open(dest, "wb") as f:
                f.write(data)
            print()
    elif op == "put":
        src, path, opts = put_args(args)
        with open(src, "rb") as f:
            data = f.read()
        name = os.path.basename(src)
        if opts:
            import media
            try:
                data, name = media.prepare(data, name, **opts)
            except media.MediaError as e:
                raise SEError(str(e))
        if not path.startswith("/"):
            raise SEError('The phone path must start with "/", e.g. "/Phone memory/Pictures/"')
        if path.endswith("/"):
            path += name
        elif os.path.splitext(name)[1] != os.path.splitext(src)[1]:
            path = os.path.splitext(path)[0] + os.path.splitext(name)[1]
        folder, fname = path.rsplit("/", 1)
        path = folder + "/" + safe_name(fname)
        print(src, "->", path, "(%s)" % human(len(data)))
        ph.write(path, data, progress)
        print()
    elif op == "rm":
        ph.delete(args[0])
    elif op == "mkdir":
        ph.mkdir(args[0])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SEError as e:
        print("Error:", e)
        sys.exit(1)
