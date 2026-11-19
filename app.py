#!/usr/bin/env python3
"""本地视频下载小工具：网页界面 + 浏览器书签一键下载。

只用 Python 标准库；不写注册表，关闭窗口或删除本文件夹即完全卸载。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
INDEX_PATH = os.path.join(BASE, "index.html")
HOST = "127.0.0.1"
PORT = 8765

DEFAULT_CONFIG = {
    "proxy": "http://127.0.0.1:6864",
    "outdir": os.path.join(BASE, "downloads"),
    "format": "bv*+ba/best",
}

_JOBS = []
_QUEUE = []
_LOCK = threading.Lock()
_COND = threading.Condition(_LOCK)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


_PREVIEW_CACHE = {}
_PREVIEW_LOCK = threading.Lock()


def preview_info(url):
    """解析视频标题、时长和封面地址，结果按链接缓存。"""
    with _PREVIEW_LOCK:
        if url in _PREVIEW_CACHE:
            return _PREVIEW_CACHE[url]

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("找不到 yt-dlp，请确认已安装并在 PATH 中")

    cfg = load_config()
    cmd = [
        ytdlp,
        "--proxy", cfg.get("proxy", ""),
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        url,
    ]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or "").splitlines() if l.strip()]
        raise RuntimeError(lines[-1][:200] if lines else "解析失败")

    data = json.loads(proc.stdout or "{}")
    # 从 thumbnails 列表里挑分辨率最高的那张，拿不到列表时退回 thumbnail 字段
    best = None
    for t in data.get("thumbnails") or []:
        if not t.get("url"):
            continue
        if best is None or (t.get("width") or 0) * (t.get("height") or 0) > \
                (best.get("width") or 0) * (best.get("height") or 0):
            best = t
    info = {
        "title": data.get("title") or "",
        "duration": data.get("duration") or 0,
        "thumbnail": (best or {}).get("url") or data.get("thumbnail") or "",
        "width": (best or {}).get("width") or 0,
        "height": (best or {}).get("height") or 0,
        "id": data.get("id") or "",
        "uploader": data.get("uploader") or "",
    }
    with _PREVIEW_LOCK:
        _PREVIEW_CACHE[url] = info
    return info


def fetch_image(src):
    """通过同一个代理抓取封面图片，避免浏览器跨域/防盗链问题。"""
    cfg = load_config()
    proxy = cfg.get("proxy") or ""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=30) as resp:
        return resp.read(), resp.headers.get("Content-Type", "image/jpeg")


def save_thumbnail(url):
    """把封面存进保存目录，返回文件名。"""
    info = preview_info(url)
    if not info["thumbnail"]:
        raise RuntimeError("没有找到封面")
    data, ctype = fetch_image(info["thumbnail"])
    outdir = load_config().get("outdir") or DEFAULT_CONFIG["outdir"]
    os.makedirs(outdir, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]', "_", info["title"] or "cover").strip()[:80]
    if info["id"]:
        name += f" [{info['id']}]"
    ext = {
        "image/webp": ".webp",
        "image/png": ".png",
        "image/gif": ".gif",
    }.get((ctype or "").split(";")[0].strip().lower(), ".jpg")
    path = os.path.join(outdir, name + ext)
    with open(path, "wb") as f:
        f.write(data)
    return path


def add_jobs(raw_text, mode="video"):
    """把一段文本里的链接全部加入队列，返回新增任务的 id 列表。"""
    mode = "audio" if mode == "audio" else "video"
    urls = [u.strip() for u in re.split(r"\s+", raw_text or "") if u.strip()]
    added = []
    for url in urls:
        if not re.match(r"^https?://", url, re.I):
            continue
        job = {
            "id": uuid.uuid4().hex[:8],
            "url": url,
            "title": url,
            "mode": mode,
            "status": "queued",
            "percent": 0.0,
            "speed": "",
            "file": "",
            "error": "",
            "created": time.time(),
        }
        with _COND:
            _JOBS.append(job)
            _QUEUE.append(job)
            _COND.notify()
        added.append(job["id"])
    return added


def _run_job(job):
    cfg = load_config()
    outdir = cfg.get("outdir") or DEFAULT_CONFIG["outdir"]
    os.makedirs(outdir, exist_ok=True)

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        job["status"] = "error"
        job["error"] = "找不到 yt-dlp，请确认已安装并在 PATH 中"
        return

    cmd = [
        ytdlp,
        "--proxy", cfg.get("proxy", ""),
        "--no-playlist",
        "--no-colors",
        "--newline",
        "--continue",
        "--retries", "10",
        "--fragment-retries", "10",
        "-P", outdir,
        "-o", "%(title)s [%(id)s].%(ext)s",
    ]
    if job.get("mode") == "audio":
        # 只要音频：取最佳音轨并转成 mp3
        cmd += ["-f", "ba/best", "-x", "--audio-format", "mp3"]
    else:
        cmd += ["-f", cfg.get("format") or DEFAULT_CONFIG["format"],
                "--merge-output-format", "mp4"]
    cmd.append(job["url"])
    job["status"] = "downloading"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for line in proc.stdout:
        line = line.rstrip("\r\n")
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            job["percent"] = float(m.group(1))
            s = re.search(r"\bat\s+(\S+/s)", line)
            if s:
                job["speed"] = s.group(1)
        if line.startswith("[download] Destination:"):
            job["file"] = line.split("Destination:", 1)[1].strip()
        elif "Merging formats into" in line:
            job["file"] = line.split("Merging formats into", 1)[1].strip().strip('"')
        elif line.startswith("[ExtractAudio] Destination:"):
            job["file"] = line.split("Destination:", 1)[1].strip()
        elif "has already been downloaded" in line:
            job["file"] = line.split("[download]", 1)[-1].split("has already been downloaded")[0].strip()
        elif line.startswith("ERROR"):
            job["error"] = line
        if job["file"]:
            job["title"] = os.path.basename(job["file"])

    code = proc.wait()
    if code == 0:
        job["status"] = "done"
        job["percent"] = 100.0
        job["speed"] = ""
    else:
        job["status"] = "error"
        job["error"] = job["error"] or f"yt-dlp 退出码 {code}"


def _worker():
    while True:
        with _COND:
            while not _QUEUE:
                _COND.wait()
            job = _QUEUE.pop(0)
        try:
            if job.get("mode") == "frame":
                _run_frame(job)
            else:
                _run_job(job)
        except Exception as exc:  # 兜底，避免工作线程退出
            job["status"] = "error"
            job["error"] = str(exc)


def _run_frame(job):
    """下载指定时间点附近的一小段视频，再用 ffmpeg 截取该帧为 jpg。"""
    cfg = load_config()
    outdir = cfg.get("outdir") or DEFAULT_CONFIG["outdir"]
    os.makedirs(outdir, exist_ok=True)

    ytdlp = shutil.which("yt-dlp")
    ffmpeg = shutil.which("ffmpeg")
    if not ytdlp or not ffmpeg:
        job["status"] = "error"
        job["error"] = "需要 yt-dlp 和 ffmpeg，请确认已安装并在 PATH 中"
        return

    secs = max(0.0, float(job.get("ts") or 0))
    start = max(0.0, secs - 5.0)
    tmp = tempfile.mkdtemp(prefix="frame_", dir=outdir)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    try:
        job["status"] = "downloading"
        cmd = [
            ytdlp,
            "--proxy", cfg.get("proxy", ""),
            "-f", "bv*",
            "--download-sections", f"*{start:.2f}-{start + 10:.2f}",
            "--force-keyframes-at-cuts",
            "--no-playlist",
            "--no-colors",
            "--newline",
            "-P", tmp,
            "-o", "seg.%(ext)s",
            job["url"],
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in proc.stdout:
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                job["percent"] = float(m.group(1)) * 0.8
            elif line.startswith("ERROR"):
                job["error"] = line
        if proc.wait() != 0:
            job["status"] = "error"
            job["error"] = job["error"] or "片段下载失败"
            return

        seg = next((os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("seg.")), None)
        if not seg:
            job["status"] = "error"
            job["error"] = "没有拿到视频片段"
            return

        try:
            info = preview_info(job["url"])
        except Exception:
            info = {"title": "", "id": ""}
        if info.get("duration") and secs > info["duration"]:
            job["status"] = "error"
            job["error"] = "时间点超出视频时长"
            return

        name = re.sub(r'[\\/:*?"<>|]', "_", info.get("title") or "frame").strip()[:80]
        h, m, s = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
        stamp = f"{h:02d}m{m:02d}s{s:02d}" if h else f"{m:02d}m{s:02d}s"
        if info.get("id"):
            name += f" [{info['id']}]"
        out_path = os.path.join(outdir, f"{name} {stamp}.jpg")

        job["percent"] = 90
        ff = [ffmpeg, "-y", "-ss", f"{secs - start:.3f}", "-i", seg,
              "-frames:v", "1", "-q:v", "2", out_path]
        res = subprocess.run(ff, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if res.returncode != 0 or not os.path.exists(out_path):
            job["status"] = "error"
            job["error"] = "ffmpeg 截帧失败"
            return

        job["file"] = out_path
        job["title"] = os.path.basename(out_path)
        job["speed"] = ""
        job["percent"] = 100.0
        job["status"] = "done"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


DL_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ margin: 0; height: 100vh; display: grid; place-items: center;
         background: #0f1115; color: #e8eaed;
         font: 14px "Segoe UI", "Microsoft YaHei", sans-serif; }}
  .box {{ text-align: center; }}
  .icon {{ font-size: 30px; }}
</style>
<div class="box"><div class="icon">{icon}</div><p>{msg}</p></div>
<script>setTimeout(function () {{ window.close(); }}, 1500);</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-downloader-local"

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send(self, code, ctype, body):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "r", encoding="utf-8") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(500, "text/plain; charset=utf-8", "index.html 缺失")
            return

        if path == "/api/jobs":
            with _LOCK:
                jobs = sorted(_JOBS, key=lambda j: j["created"], reverse=True)[:60]
            self._json({"jobs": jobs, "config": load_config(), "port": PORT})
            return

        if path == "/api/preview":
            url = (parse_qs(parsed.query).get("url") or [""])[0].strip()
            if not re.match(r"^https?://", url, re.I):
                self._json({"ok": False, "error": "链接无效"})
                return
            try:
                info = preview_info(url)
                self._json({"ok": True, **info})
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "error": "解析超时"})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
            return

        if path == "/api/thumb":
            src = (parse_qs(parsed.query).get("src") or [""])[0]
            if not re.match(r"^https?://", src, re.I):
                self._send(404, "text/plain", "bad src")
                return
            try:
                data, ctype = fetch_image(src)
                self._send(200, ctype, data)
            except Exception:
                self._send(502, "text/plain; charset=utf-8", "cover fetch failed")
            return

        if path == "/dl":
            url = (parse_qs(parsed.query).get("url") or [""])[0].strip()
            if not re.match(r"^https?://", url, re.I):
                self._send(200, "text/html; charset=utf-8",
                           DL_PAGE.format(title="链接无效", icon="!", msg="没有收到有效的视频链接"))
                return
            add_jobs(url, (parse_qs(parsed.query).get("mode") or ["video"])[0])
            self._send(200, "text/html; charset=utf-8",
                       DL_PAGE.format(title="已加入", icon="\u2713", msg="已加入下载队列，可以关闭本窗口"))
            return

        self._send(404, "text/plain; charset=utf-8", "not found")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/download":
            ids = add_jobs(body.get("urls") or body.get("url") or "", body.get("mode") or "video")
            self._json({"added": ids})
            return

        if path == "/api/frame":
            url = (body.get("url") or "").strip()
            try:
                ts = float(body.get("ts") or 0)
            except (TypeError, ValueError):
                ts = -1
            if not re.match(r"^https?://", url, re.I) or ts < 0:
                self._json({"ok": False, "error": "链接或时间点无效"})
                return
            h, m, s = int(ts // 3600), int(ts % 3600 // 60), int(ts % 60)
            stamp = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            job = {
                "id": uuid.uuid4().hex[:8],
                "url": url,
                "title": f"截帧 {stamp} · {url}",
                "mode": "frame",
                "ts": ts,
                "status": "queued",
                "percent": 0.0,
                "speed": "",
                "file": "",
                "error": "",
                "created": time.time(),
            }
            with _COND:
                _JOBS.append(job)
                _QUEUE.append(job)
                _COND.notify()
            self._json({"ok": True, "id": job["id"]})
            return

        if path == "/api/config":
            cfg = load_config()
            for key in ("proxy", "outdir", "format"):
                val = (body.get(key) or "").strip()
                if val:
                    cfg[key] = val
            save_config(cfg)
            self._json({"ok": True, "config": cfg})
            return

        if path == "/api/save-thumb":
            url = (body.get("url") or "").strip()
            if not re.match(r"^https?://", url, re.I):
                self._json({"ok": False, "error": "链接无效"})
                return
            try:
                saved = save_thumbnail(url)
                self._json({"ok": True, "name": os.path.basename(saved)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
            return

        if path == "/api/open":
            outdir = load_config().get("outdir") or DEFAULT_CONFIG["outdir"]
            os.makedirs(outdir, exist_ok=True)
            os.startfile(outdir)
            self._json({"ok": True})
            return

        if path == "/api/clear":
            with _COND:
                _JOBS[:] = [j for j in _JOBS if j["status"] in ("queued", "downloading")]
            self._json({"ok": True})
            return

        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        self._send(404, "text/plain; charset=utf-8", "not found")


def main():
    url = f"http://{HOST}:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"端口 {PORT} 已被占用，可能程序已经在运行，正在打开浏览器…")
        webbrowser.open(url)
        return

    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    threading.Thread(target=_worker, daemon=True).start()
    print(f"yt-downloader 已启动：{url}")
    print("关闭这个窗口（或点网页上的「退出」）即可停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
