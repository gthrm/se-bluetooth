"""Shrink pictures, music and video so they suit a Sony Ericsson W810i, and make photo thumbnails."""
# SPDX-License-Identifier: GPL-2.0-or-later
import ctypes
import functools
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time

SCREEN = (176, 220)  # W810i display, width x height
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".heic", ".heif", ".tif", ".tiff", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".opus"}
VIDEO_EXT = {".3gp", ".3g2", ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".mpg", ".mpeg"}
PHONE_VIDEO_EXT = {".3gp", ".3g2", ".mp4"}  # what the phone's video player opens
MP3_BITRATES = (128, 96, 64, 48, 32)
FFMPEG_TIMEOUT = 600  # seconds
IMAGE_MODES = ("fit", "fill")
# Like the phone's own camera clips: MPEG-4 Simple Profile 176x144 + AMR-NB 12.2 kbps, 8 kHz mono
VIDEO_SIZE = (176, 144)
VIDEO_QUALITY = {
    "small": {"kbps": 64, "fps": 12},    # about 0.8 MB per minute in practice
    "normal": {"kbps": 128, "fps": 15},  # about 1.3 MB per minute in practice
}


class MediaError(Exception):
    pass


def images_supported():
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def find_ffmpeg():
    # apps started from Finder don't get Homebrew's PATH, so look there too
    for path in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if path and os.path.exists(path):
            return path
    return None


def _ext(name):
    return os.path.splitext(name)[1].lower()


def image_for_screen(data, name, mode="fit", quality=85):
    """Return (data, name) resized for the phone screen, as a baseline JPEG.

    mode "fit":  the whole picture fits inside 176x220 (only ever shrinks)
    mode "fill": scaled and centre-cropped to exactly 176x220, for wallpapers
    Animated GIFs are returned unchanged.
    """
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return data, name
        # the phone shows a JPEG as stored: progressive ones not at all, EXIF rotation ignored
        phone_ready = (im.format == "JPEG" and not im.info.get("progressive") and not im.info.get("progression")
                       and im.getexif().get(0x0112, 1) == 1)
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise MediaError("Can't read %s: not a picture, or a format that isn't supported" % name)

    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    small_already = im.width <= SCREEN[0] and im.height <= SCREEN[1]
    if mode == "fill":
        im = ImageOps.fit(im, SCREEN, Image.LANCZOS)
    else:
        im.thumbnail(SCREEN, Image.LANCZOS)

    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)  # baseline: old phones can't do progressive
    out = out.getvalue()
    if mode == "fit" and small_already and phone_ready and len(out) >= len(data):
        return data, name
    return out, os.path.splitext(name)[0] + ".jpg"


def _ffprobe(ffmpeg):
    path = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
    return path if os.path.exists(path) else None


def _probe(ffmpeg, path):
    """{'known': bool, 'duration': s, 'video': (codec, w, h) or None, 'audio': codec or None}"""
    info = {"known": False, "duration": None, "video": None, "audio": None}
    ffprobe = _ffprobe(ffmpeg)
    if not ffprobe:
        return info
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-of", "json", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height", path],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return info  # unknown: the conversion itself will report what's wrong
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return info
    info["known"] = r.returncode == 0
    try:
        info["duration"] = float(j.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        pass
    for st in j.get("streams", []):
        if st.get("codec_type") == "video" and info["video"] is None:
            info["video"] = (st.get("codec_name"), st.get("width") or 0, st.get("height") or 0)
        elif st.get("codec_type") == "audio" and info["audio"] is None:
            info["audio"] = st.get("codec_name")
    return info


def _duration(ffmpeg, path):
    return _probe(ffmpeg, path)["duration"]


def _run_ffmpeg(ffmpeg, args, dst, name, duration=None, on_progress=None, timeout=FFMPEG_TIMEOUT):
    """Run ffmpeg with args, writing dst; report progress and raise MediaError on failure."""
    tmp = os.path.dirname(dst)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + args + \
          ["-progress", "pipe:1", "-nostats", dst]
    log = os.path.join(tmp, os.path.basename(dst) + ".log")
    # errors go to a file, not a pipe: a broken input can log more than a pipe holds
    with open(log, "w") as errf, \
            subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True) as p:
        killer = threading.Timer(timeout, p.kill)
        killer.start()
        try:
            for line in p.stdout:
                # out_time_us is microseconds of media written so far
                if on_progress and duration and line.startswith("out_time_us="):
                    try:
                        on_progress(min(int(line.split("=", 1)[1]) / 1e6 / duration, 1.0))
                    except ValueError:
                        pass
        finally:
            killer.cancel()
    if p.returncode != 0 or not os.path.exists(dst):
        if p.returncode == -signal.SIGKILL:
            msg = "timed out after %d s" % timeout
        else:
            with open(log) as f:
                msg = (f.read().strip().splitlines() or ["unknown error"])[-1]
            msg = re.sub(r"^\[[^\]]* @ 0x[0-9a-f]+\] ", "", msg)  # drop ffmpeg's "[muxer @ 0x...]" prefix
        raise MediaError("Can't convert %s: %s" % (name, msg))
    with open(dst, "rb") as f:
        return f.read()


def _need_ffmpeg(what):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise MediaError("%s needs ffmpeg: brew install ffmpeg" % what)
    return ffmpeg


@functools.lru_cache(maxsize=None)
def _encoders(ffmpeg):
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(line.split()[1] for line in r.stdout.splitlines() if len(line.split()) > 1)


def _h264_encoder(ffmpeg):
    for enc in ("libx264", "h264_videotoolbox"):
        if enc in _encoders(ffmpeg):
            return enc
    return None


def _scaled(on_progress, lo, hi):
    """Map a 0..1 progress callback onto the lo..hi part of the overall job."""
    return (lambda f: on_progress(lo + (hi - lo) * f)) if on_progress else None


def mp3_for_phone(data, name, kbps=64, on_progress=None):
    """Return (data, name) re-encoded as MP3 at kbps (44.1 kHz; mono at 48 kbps and below).

    Cover art is dropped, tags are kept as ID3v2.3. If the source is already an MP3
    that would only get bigger, it is returned unchanged.
    on_progress(fraction) is called while encoding when the duration is known.
    """
    ffmpeg = _need_ffmpeg("MP3 compression")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + _ext(name))
        with open(src, "wb") as f:
            f.write(data)
        args = ["-i", src, "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "%dk" % kbps,
                "-ar", "44100", "-id3v2_version", "3"]
        if kbps <= 48:
            args += ["-ac", "1"]
        duration = _duration(ffmpeg, src) if on_progress else None
        out = _run_ffmpeg(ffmpeg, args, os.path.join(tmp, "out.mp3"), name, duration, on_progress)
    if _ext(name) == ".mp3" and len(out) >= len(data):
        return data, name
    return out, os.path.splitext(name)[0] + ".mp3"


# --- AMR-NB (the phone's video sound) via libopencore-amrnb -------------------

@functools.lru_cache(maxsize=1)
def _amrnb():
    for path in ("/opt/homebrew/lib/libopencore-amrnb.dylib", "/usr/local/lib/libopencore-amrnb.dylib"):
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue  # wrong architecture or broken install
        lib.Encoder_Interface_init.restype = ctypes.c_void_p
        lib.Encoder_Interface_init.argtypes = [ctypes.c_int]
        lib.Encoder_Interface_Encode.restype = ctypes.c_int
        lib.Encoder_Interface_Encode.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_short),
                                                 ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        lib.Encoder_Interface_exit.argtypes = [ctypes.c_void_p]
        return lib
    return None


def video_supported():
    return find_ffmpeg() is not None and _amrnb() is not None


def video_preview_supported():
    ffmpeg = find_ffmpeg()
    return ffmpeg is not None and _h264_encoder(ffmpeg) is not None


def encode_amr(pcm, on_progress=None):
    """8 kHz mono signed 16-bit little-endian PCM -> .amr file (12.2 kbps)."""
    lib = _amrnb()
    if lib is None:
        raise MediaError("Video conversion needs the AMR encoder: brew install opencore-amr")
    MR122, FRAME = 7, 320  # mode 12.2 kbps; 160 samples per 20 ms frame
    state = lib.Encoder_Interface_init(0)
    if not state:
        raise MediaError("The AMR encoder failed to start")
    out = bytearray(b"#!AMR\n")
    buf = (ctypes.c_ubyte * 64)()
    try:
        for i in range(0, len(pcm), FRAME):
            samples = (ctypes.c_short * 160).from_buffer_copy(pcm[i:i + FRAME].ljust(FRAME, b"\0"))
            n = lib.Encoder_Interface_Encode(state, MR122, samples, buf, 0)
            out += bytes(buf[:n])
            if on_progress and i % (FRAME * 500) == 0:
                on_progress(i / len(pcm))
    finally:
        lib.Encoder_Interface_exit(state)
    return bytes(out)


def _phone_ready(info, name):
    """True if the file already is the kind of 3GP the phone records."""
    if _ext(name) not in (".3gp", ".3g2") or not info["video"]:
        return False
    codec, w, h = info["video"]
    return codec in ("mpeg4", "h263") and 0 < w <= VIDEO_SIZE[0] and 0 < h <= VIDEO_SIZE[1] \
        and info["audio"] in (None, "amr_nb")


def video_for_phone(data, name, quality="normal", on_progress=None):
    """Return (data, name) converted to a 3GP the phone plays: MPEG-4 176x144 + AMR-NB.

    The picture is letterboxed to keep its shape; rotation metadata is applied.
    A file that already is a phone-ready 3GP is returned unchanged.
    """
    ffmpeg = _need_ffmpeg("Video conversion")
    if _amrnb() is None:
        raise MediaError("Video conversion needs the AMR encoder: brew install opencore-amr")
    preset = VIDEO_QUALITY[quality]
    w, h = VIDEO_SIZE
    deadline = time.monotonic() + FFMPEG_TIMEOUT  # one budget for the whole conversion

    def left():
        return max(1.0, deadline - time.monotonic())

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + _ext(name))
        with open(src, "wb") as f:
            f.write(data)
        info = _probe(ffmpeg, src)
        if info["known"] and not info["video"]:
            raise MediaError("Can't convert %s: it has no video" % name)
        if _phone_ready(info, name):
            return data, name
        # sound: decode to 8 kHz mono PCM, encode AMR ourselves (ffmpeg has no AMR encoder)
        amr = None
        if info["audio"] or not info["known"]:
            try:
                pcm = _run_ffmpeg(ffmpeg, ["-i", src, "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le"],
                                  os.path.join(tmp, "sound.pcm"), name, info["duration"],
                                  _scaled(on_progress, 0, 0.1), timeout=left())
            except MediaError:
                if info["known"]:
                    raise  # there is a sound track and it failed: don't upload a silent video
                pcm = b""  # couldn't probe: most likely there is no sound
            if pcm:
                amr = os.path.join(tmp, "sound.amr")
                with open(amr, "wb") as f:
                    f.write(encode_amr(pcm, _scaled(on_progress, 0.1, 0.15)))
        args = ["-i", src]
        if amr:
            args += ["-i", amr]
        args += ["-map", "0:v:0"] + (["-map", "1:a:0", "-c:a", "copy"] if amr else ["-an"])
        args += ["-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1"
                 % (w, h, w, h),
                 "-r", str(preset["fps"]), "-c:v", "mpeg4", "-vtag", "mp4v",
                 "-b:v", "%dk" % preset["kbps"], "-maxrate", "%dk" % (preset["kbps"] * 3 // 2),
                 "-bufsize", "%dk" % (preset["kbps"] * 2), "-pix_fmt", "yuv420p",
                 "-map_metadata", "-1", "-f", "3gp"]
        out = _run_ffmpeg(ffmpeg, args, os.path.join(tmp, "out.3gp"), name, info["duration"],
                          _scaled(on_progress, 0.15 if amr else 0, 1), timeout=left())
    # no "keep the original if smaller" here: _phone_ready said the phone can't play it
    return out, os.path.splitext(name)[0] + ".3gp"


def video_preview(data, name, poster=False):
    """Make a phone video playable in the browser: H.264/AAC MP4, or a JPEG frame if poster."""
    ffmpeg = _need_ffmpeg("Video preview")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + _ext(name))
        with open(src, "wb") as f:
            f.write(data)
        if poster:
            # thumbnail filter picks a typical frame; the first one is often black
            return _run_ffmpeg(ffmpeg, ["-i", src, "-vf", "thumbnail=60", "-frames:v", "1",
                                        "-f", "image2", "-c:v", "mjpeg"],
                               os.path.join(tmp, "poster.jpg"), name)
        enc = _h264_encoder(ffmpeg)
        if not enc:
            raise MediaError("Video preview needs an ffmpeg with H.264 (libx264)")
        speed = ["-preset", "veryfast"] if enc == "libx264" else ["-b:v", "1M"]
        # H.264 4:2:0 needs even width and height
        return _run_ffmpeg(ffmpeg, ["-i", src, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", enc] + speed +
                           ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
                            "-f", "mp4"],
                           os.path.join(tmp, "preview.mp4"), name)


def exif_thumbnail(data):
    """The JPEG thumbnail a camera stores in the EXIF block at the start of a photo, or None.

    Works on the first few KB of a file, so a preview doesn't need the whole photo.
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            return _ifd1_jpeg(data[i + 10:i + 2 + size])
        if marker in (0xDA, 0xD9):
            return None
        i += 2 + size
    return None


def _ifd1_jpeg(tiff):
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    order = "little" if tiff[:2] == b"II" else "big"
    u16 = lambda o: int.from_bytes(tiff[o:o + 2], order)  # noqa: E731
    u32 = lambda o: int.from_bytes(tiff[o:o + 4], order)  # noqa: E731
    try:
        ifd0 = u32(4)
        ifd1 = u32(ifd0 + 2 + 12 * u16(ifd0))
        if not ifd1 or ifd1 + 2 > len(tiff):
            return None
        offset = length = None
        for k in range(u16(ifd1)):
            e = ifd1 + 2 + 12 * k
            if u16(e) == 0x0201:
                offset = u32(e + 8)
            elif u16(e) == 0x0202:
                length = u32(e + 8)
    except IndexError:
        return None
    if offset is None or not length or offset + length > len(tiff):
        return None
    thumb = tiff[offset:offset + length]
    return thumb if thumb.startswith(b"\xff\xd8") else None


def sniff_image(data):
    """MIME type of an image from its first bytes, or None."""
    for magic, mime in ((b"GIF8", "image/gif"), (b"\xff\xd8\xff", "image/jpeg"),
                        (b"\x89PNG", "image/png"), (b"BM", "image/bmp")):
        if data.startswith(magic):
            return mime
    return None


def kind(name):
    """'picture', 'music', 'video', 'theme' or None, from the file extension."""
    ext = _ext(name)
    if ext == ".thm":
        return "theme"
    if ext in IMAGE_EXT:
        return "picture"
    if ext in AUDIO_EXT or ext in (".mid", ".midi", ".amr"):
        return "music"
    if ext in PHONE_VIDEO_EXT:
        return "video"
    return None


def prepare(data, name, image_mode=None, mp3_kbps=None, video_quality=None, on_progress=None):
    """Apply the requested conversions to one file; other files pass through."""
    ext = _ext(name)
    if video_quality and ext in VIDEO_EXT:
        return video_for_phone(data, name, video_quality, on_progress)
    if image_mode and ext in IMAGE_EXT:
        return image_for_screen(data, name, image_mode)
    if mp3_kbps and ext in AUDIO_EXT:
        return mp3_for_phone(data, name, mp3_kbps, on_progress)
    return data, name
