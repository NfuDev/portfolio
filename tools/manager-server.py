"""Local server for manager.html.

Start it with start-manager.bat (Windows) or ./start-manager.sh (Mac/Linux), or directly:
    python tools/manager-server.py

What it does (everything stays on your computer, it only listens on 127.0.0.1):
  * serves the whole portfolio folder, so  /manager.html  and  /index.html  both work
  * saves portfolio.json when you edit something in the manager
  * Browse -> image : converts it to AVIF (+720px thumbnail) into Resources/img/  (tools/convert-images.py)
  * Browse -> video : encodes H.264 (+ optional AV1) into Resources/Videos/ and makes a poster frame
                      (tools/encode-videos.py + the poster code of convert-images.py)

The conversion code is the same code the command-line scripts use, so results are identical.
Needs: Python 3.9+, Pillow >= 11.2 (pip install pillow) and ffmpeg on PATH for videos.
"""

import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
DATA = ROOT / "portfolio.json"
IMG_DIR = ROOT / "Resources" / "img"
VID_DIR = ROOT / "Resources" / "Videos"
BACKUP_DIR = ROOT / ".manager-backups"

HOST = "127.0.0.1"
FIRST_PORT = 8765
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv"}

mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("video/mp4", ".mp4")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Re-use the existing command-line tools (their file names contain a hyphen,
# so they are loaded by path instead of "import")
# --------------------------------------------------------------------------

_tool_cache = {}


def load_tool(filename):
    if filename not in _tool_cache:
        spec = importlib.util.spec_from_file_location(filename.replace("-", "_").removesuffix(".py"), TOOLS / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _tool_cache[filename] = module
    return _tool_cache[filename]


def image_tool():
    try:
        return load_tool("convert-images.py")
    except ImportError:
        raise ApiError("Pillow is not installed. Close this window and run:  python -m pip install --upgrade pillow", 500)


def check_status():
    """What works on this machine? The manager shows this in its header."""
    s = {"server": True, "pillow": False, "avif": False, "ffmpeg": False, "av1": False, "problems": []}
    try:
        import PIL
        from PIL import features

        s["pillow"] = True
        s["pillowVersion"] = PIL.__version__
        s["avif"] = bool(features.check("avif"))
        if not s["avif"]:
            s["problems"].append("Your Pillow cannot write AVIF. Run:  python -m pip install --upgrade pillow")
    except ImportError:
        s["problems"].append("Pillow is not installed. Run:  python -m pip install --upgrade pillow")

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        s["ffmpeg"] = True
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, errors="replace", timeout=20).stdout
            s["av1"] = "libaom-av1" in out
        except Exception:
            pass
        if not s["av1"]:
            s["problems"].append("This ffmpeg has no AV1 encoder (libaom-av1): videos will be made as H.264 only.")
    else:
        s["problems"].append("ffmpeg was not found on PATH, so videos cannot be converted (images work fine). Install ffmpeg from ffmpeg.org.")
    return s


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------


def clean_stem(filename, fallback):
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename).stem).strip("-_")
    return (stem or fallback)[:60]


def unique_image_stem(stem):
    stem = stem.lower()
    candidate, n = stem, 2
    while (IMG_DIR / f"{candidate}.avif").exists() or (IMG_DIR / f"{candidate}-720.avif").exists():
        candidate = f"{stem}-{n}"
        n += 1
    return candidate


def unique_video_stem(stem):
    candidate, n = stem, 2
    while any((VID_DIR / f"{candidate}{suffix}").exists() for suffix in (".h264.mp4", ".av1.mp4")) or (
        IMG_DIR / f"{candidate.lower()}-poster.avif"
    ).exists():
        candidate = f"{stem}-{n}"
        n += 1
    return candidate


# --------------------------------------------------------------------------
# portfolio.json
# --------------------------------------------------------------------------

_save_lock = threading.Lock()
_backed_up_this_session = False


def read_portfolio():
    if not DATA.exists():
        raise ApiError("portfolio.json was not found next to manager.html", 404)
    return json.loads(DATA.read_text(encoding="utf-8"))


def write_portfolio(data):
    global _backed_up_this_session
    if not isinstance(data, dict) or "profile" not in data:
        raise ApiError("That does not look like a portfolio.json (no 'profile' section). Nothing was saved.")
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    with _save_lock:
        if DATA.exists():
            if DATA.read_text(encoding="utf-8").replace("\r\n", "\n") == text:
                return {"saved": False}
            if not _backed_up_this_session:
                BACKUP_DIR.mkdir(exist_ok=True)
                shutil.copy2(DATA, BACKUP_DIR / f"portfolio-{time.strftime('%Y%m%d-%H%M%S')}.json")
                for old in sorted(BACKUP_DIR.glob("portfolio-*.json"))[:-15]:
                    old.unlink(missing_ok=True)
                _backed_up_this_session = True
        tmp = DATA.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, DATA)
    return {"saved": True}


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def convert_uploaded_image(filename, src_path_factory):
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ApiError(f"'{ext or filename}' is not an image type I can convert (png, jpg, webp, gif, bmp, tiff, avif).")
    tool = image_tool()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    stem = unique_image_stem(clean_stem(filename, "image"))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{stem}{ext}"
        src_path_factory(src)
        try:
            info = tool.convert_image(src)
        except Exception as e:  # corrupt file, unsupported colour mode, ...
            raise ApiError(f"Could not convert '{filename}': {e}")
    return {"src": info["src"], "w": info["w"], "h": info["h"]}


# --------------------------------------------------------------------------
# Videos (long running -> background job the page polls)
# --------------------------------------------------------------------------

JOBS = {}
_jobs_lock = threading.Lock()


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:10]
        self.state = "running"  # running | done | error
        self.label = "Starting"
        self.pct = None  # 0..100 of the current step, or None when unknown
        self.step = 0
        self.steps = 1
        self.result = None
        self.error = None
        self.created = time.time()

    def view(self):
        return {k: getattr(self, k) for k in ("id", "state", "label", "pct", "step", "steps", "result", "error")}


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, errors="replace", timeout=30,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def run_ffmpeg(src, dst, codec, duration, on_pct):
    """Same command as tools/encode-videos.py, plus -progress so the page can show a bar."""
    enc = load_tool("encode-videos.py")
    tmp = dst.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y", "-i", str(src),
           *enc.COMMON_IN, *codec, *enc.AUDIO, *enc.OUT_FLAGS, str(tmp)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail = []
    for line in proc.stdout:
        line = line.strip()
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m:
            if duration:
                on_pct(min(99, int(int(m.group(1)) / 1_000_000 / duration * 100)))
        elif line and "=" not in line.split(" ")[0]:
            tail.append(line)
            tail = tail[-6:]
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise ApiError("ffmpeg failed: " + " | ".join(tail) if tail else "ffmpeg failed")
    tmp.replace(dst)


def make_poster_frame(h264_path, stem):
    tool = image_tool()
    try:
        return tool.make_poster(h264_path)
    except Exception:
        # Very short clip: there is no frame at 2s, so grab the first one.
        tool.POSTER_AT[stem] = 0
        return tool.make_poster(h264_path)


def video_job(job, tmp_dir, src_path, stem, want_av1, status):
    enc = load_tool("encode-videos.py")
    h264, av1 = VID_DIR / f"{stem}.h264.mp4", VID_DIR / f"{stem}.av1.mp4"
    made = []
    try:
        do_av1 = want_av1 and status["av1"]
        job.steps = 3 if do_av1 else 2
        duration = probe_duration(src_path)

        def set_pct(p):
            job.pct = p

        job.step, job.label, job.pct = 1, "Encoding H.264", 0
        run_ffmpeg(src_path, h264, enc.H264, duration, set_pct)
        made.append(h264)

        job.step, job.label, job.pct = 2, "Making poster image", None
        poster = make_poster_frame(h264, stem)

        result = {"type": "video", "src": f"Resources/Videos/{h264.name}", "poster": poster["src"], "w": poster["w"], "h": poster["h"], "autoplay": True}
        if do_av1:
            job.step, job.label, job.pct = 3, "Encoding AV1 (smaller file, slow)", 0
            run_ffmpeg(src_path, av1, enc.AV1, duration, set_pct)
            made.append(av1)
            result["av1"] = f"Resources/Videos/{av1.name}"
        elif want_av1:
            result["note"] = "No AV1 encoder in this ffmpeg, so only the H.264 version was made."

        job.result, job.pct, job.state, job.label = result, 100, "done", "Done"
    except ApiError as e:
        for p in made:
            p.unlink(missing_ok=True)
        job.error, job.state = str(e), "error"
    except Exception as e:
        for p in made:
            p.unlink(missing_ok=True)
        job.error, job.state = f"Unexpected error: {e}", "error"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def start_video_job(filename, receive):
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise ApiError(f"'{ext or filename}' is not a video type I can convert (mp4, mov, mkv, webm, avi...).")
    status = check_status()
    if not status["ffmpeg"]:
        raise ApiError("ffmpeg was not found on PATH, so videos cannot be converted. Install it from ffmpeg.org and restart the manager.", 500)
    VID_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    stem = unique_video_stem(clean_stem(filename, "video"))
    tmp_dir = Path(tempfile.mkdtemp(prefix="portfolio-video-"))
    src_path = tmp_dir / f"source{ext}"
    try:
        receive(src_path)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return stem, tmp_dir, src_path, status


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    server_version = "PortfolioManager/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # ---- plumbing
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/") and not self.path.startswith("/api/job"):
            sys.stdout.write(f"  {self.command} {self.path.split('?')[0]}  ->  {args[1] if len(args) > 1 else ''}\n")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body_to(self, dest: Path):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError("The upload was empty.")
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    raise ApiError("The upload was cut off. Try again.")
                f.write(chunk)
                remaining -= len(chunk)

    def guard_api(self, write):
        """Only this machine, and only requests our own page makes (blocks other websites from poking the API)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            raise ApiError("Forbidden", 403)
        if write:
            if self.headers.get("X-Manager") != "1":
                raise ApiError("Forbidden", 403)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ALLOWED_HOSTS:
                raise ApiError("Forbidden", 403)

    # ---- routing
    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return super().do_GET()
        try:
            self.guard_api(write=False)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/api/status":
                return self.send_json(check_status())
            if url.path == "/api/portfolio":
                return self.send_json(read_portfolio())
            if url.path == "/api/job":
                with _jobs_lock:
                    job = JOBS.get(q.get("id", ""))
                if not job:
                    raise ApiError("Unknown job", 404)
                return self.send_json(job.view())
            raise ApiError("Not found", 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:
            self.send_json({"error": f"Server error: {e}"}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            self.guard_api(write=True)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            name = os.path.basename(q.get("name", "file"))

            if url.path == "/api/portfolio":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    raise ApiError("That was not valid JSON. Nothing was saved.")
                return self.send_json(write_portfolio(data))

            if url.path == "/api/upload-image":
                result = convert_uploaded_image(name, self.read_body_to)
                print(f"  converted image  {name}  ->  {result['src']}  ({result['w']}x{result['h']})")
                return self.send_json(result)

            if url.path == "/api/upload-video":
                stem, tmp_dir, src_path, status = start_video_job(name, self.read_body_to)
                job = Job()
                with _jobs_lock:
                    for jid in [j for j, v in JOBS.items() if time.time() - v.created > 3600]:
                        del JOBS[jid]
                    JOBS[job.id] = job
                print(f"  encoding video  {name}  ->  Resources/Videos/{stem}.*")
                threading.Thread(target=video_job, args=(job, tmp_dir, src_path, stem, q.get("av1") == "1", status), daemon=True).start()
                return self.send_json({"job": job.id})

            raise ApiError("Not found", 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:
            self.send_json({"error": f"Server error: {e}"}, 500)

    # ---- static files with Range support (so <video> can seek when previewing locally)
    def send_head(self):
        self._range_left = None
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        m = re.match(r"bytes=(\d*)-(\d*)$", (rng or "").strip())
        if not m or not os.path.isfile(path) or not (m.group(1) or m.group(2)):
            return super().send_head()
        size = os.path.getsize(path)
        if m.group(1) == "":
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start, end = int(m.group(1)), min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        try:
            left = getattr(self, "_range_left", None)
            if left is None:
                return super().copyfile(source, outputfile)
            while left > 0:
                chunk = source.read(min(1 << 16, left))
                if not chunk:
                    break
                outputfile.write(chunk)
                left -= len(chunk)
        except (ConnectionError, TimeoutError):
            pass  # the browser closed the connection (e.g. seeking in a video)


def main():
    status = check_status()
    server = None
    for port in range(FIRST_PORT, FIRST_PORT + 20):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"Could not open a port between {FIRST_PORT} and {FIRST_PORT + 19}. Is another manager window already open?")

    url = f"http://{HOST}:{server.server_port}"
    print("=" * 60)
    print("  Portfolio manager is running")
    print(f"  Manager : {url}/manager.html")
    print(f"  Site    : {url}/index.html")
    print("  Keep this window open while you work. Close it (or Ctrl+C) to stop.")
    print("-" * 60)
    print(f"  Images  : {'ready' if status['pillow'] and status['avif'] else 'NOT READY'}")
    print(f"  Videos  : {'ready' if status['ffmpeg'] else 'NOT READY'}{'' if status['av1'] or not status['ffmpeg'] else ' (H.264 only, no AV1 encoder)'}")
    for p in status["problems"]:
        print(f"  ! {p}")
    print("=" * 60)

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(f"{url}/manager.html")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
