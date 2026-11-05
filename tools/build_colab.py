"""Build colab/VoiceGetter.ipynb: a one-cell notebook with the app code embedded.

The notebook does not clone the repository, so it also works when the repository is private.
Run this script again after you change files in app/, cli.py or requirements.txt:

    python tools/build_colab.py          # rebuild
    python tools/build_colab.py --check  # exit 1 if the notebook is out of date
"""

import base64
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "colab" / "VoiceGetter.ipynb"
FILES = ["requirements.txt", "cli.py", "app/__init__.py", "app/main.py", "app/pipeline.py", "app/static/index.html"]


def bundle() -> tuple[str, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in FILES:
            data = (ROOT / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    raw = gzip.compress(buf.getvalue(), mtime=0)  # mtime=0: same input -> same notebook
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()[:12]


INTRO = """\
# 🎙️ VoiceGetter

**Tap ▶ on the cell below.** After about 5 minutes it shows a **link**. Open it on your phone.

- If Colab says *"This notebook was not authored by Google"*, tap **Run anyway**.
- Keep this tab open while VoiceGetter works. Colab stops the free server after some time.
- The notebook selects a GPU automatically. If it says *no GPU*, use **Runtime → Change runtime type → T4 GPU** and tap ▶ again.
- YouTube can block Colab ("Sign in to confirm you're not a bot"). Then turn on `UPLOAD_COOKIES` and upload a `cookies.txt` file (see the README), or upload the audio file on the web page.
"""

CELL = r'''#@title ▶ Start VoiceGetter (about 5 minutes the first time)
UPLOAD_COOKIES = False  #@param {type:"boolean"}

BUNDLE = "%(bundle)s"
BUNDLE_ID = "%(bundle_id)s"

import base64, io, os, re, secrets, subprocess, sys, tarfile, time, urllib.request
from IPython.display import HTML, display

APP = "/content/voicegetter"
LOG = "/content/voicegetter.log"
TUNNEL_LOG = "/content/tunnel.log"

def step(msg):
    print(time.strftime("[%%H:%%M:%%S] ") + msg, flush=True)

def run(cmd, what, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        print((r.stdout + r.stderr)[-3000:])
        raise SystemExit(f"FAILED: {what}. Tap ▶ again. If it fails again, send the text above to the developer.")

# 1. GPU
gpu = subprocess.run("nvidia-smi -L", shell=True, capture_output=True, text=True).stdout.strip()
if gpu:
    step("GPU: " + gpu.splitlines()[0])
else:
    display(HTML("<b style='color:#d64545'>No GPU. It will work, but very slowly. "
                 "Use Runtime → Change runtime type → T4 GPU, then tap ▶ again.</b>"))

# 2. App files (embedded in this notebook)
os.makedirs(APP, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(BUNDLE)), mode="r:gz") as tar:
    try:
        tar.extractall(APP, filter="data")
    except TypeError:
        tar.extractall(APP)

# 3. Install (only the first time)
marker = f"{APP}/.installed-{BUNDLE_ID}"
if not os.path.exists(marker):
    step("Installing (3-4 minutes)...")
    run([sys.executable, "-m", "pip", "install", "-q", "-r", f"{APP}/requirements.txt"], "install")
    step("Downloading AI models (about 1 minute)...")
    run([sys.executable, "-c", "from app import pipeline as P; P.get_demucs('cpu'); P.get_encoder('cpu'); P.get_vad()"],
        "model download", cwd=APP)
    open(marker, "w").close()
if not os.path.exists("/usr/local/bin/cloudflared"):
    run(["wget", "-q", "-O", "/usr/local/bin/cloudflared",
         "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"], "tunnel download")
    os.chmod("/usr/local/bin/cloudflared", 0o755)
step("Installed.")

# 4. Cookies (optional)
env = dict(os.environ, VOICEGETTER_DATA="/content/data", PORT="8000")
if UPLOAD_COOKIES:
    from google.colab import files
    print("Choose your cookies.txt file:")
    up = files.upload()
    if up:
        os.replace(next(iter(up)), "/content/cookies.txt")
if os.path.exists("/content/cookies.txt"):
    env["VOICEGETTER_COOKIES"] = "/content/cookies.txt"
    step("Using cookies.txt")

# 5. Start the server
if not os.path.exists("/content/password.txt"):
    open("/content/password.txt", "w").write(secrets.token_urlsafe(9))
password = open("/content/password.txt").read().strip()
env["VOICEGETTER_PASSWORD"] = password
subprocess.run("pkill -f 'python -m app.main'; pkill -f 'cloudflared tunnel'", shell=True)
time.sleep(1)

def start_server():
    return subprocess.Popen([sys.executable, "-m", "app.main"], cwd=APP, env=env,
                            stdout=open(LOG, "a"), stderr=subprocess.STDOUT)

def start_tunnel():
    open(TUNNEL_LOG, "w").close()
    p = subprocess.Popen(["/usr/local/bin/cloudflared", "tunnel", "--no-autoupdate", "--url", "http://localhost:8000"],
                         stdout=open(TUNNEL_LOG, "a"), stderr=subprocess.STDOUT)
    for _ in range(90):
        time.sleep(1)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(TUNNEL_LOG).read())
        if m:
            return p, m.group(0)
    raise SystemExit("FAILED: could not make the public link. Tap ▶ again.")

server = start_server()
for _ in range(60):
    time.sleep(1)
    try:
        urllib.request.urlopen("http://localhost:8000/api/config", timeout=2)
        break
    except Exception:
        if server.poll() is not None:
            print(open(LOG).read()[-3000:])
            raise SystemExit("FAILED: the server stopped. Tap ▶ again.")

def show(url):
    link = f"{url}/#key={password}"
    display(HTML(f"""
      <div style="font:16px system-ui;padding:16px;border-radius:12px;background:#7c4dff22;margin:8px 0">
        <div style="font-size:20px;font-weight:700;margin-bottom:8px">✅ VoiceGetter is ready</div>
        <a href="{link}" target="_blank" style="display:inline-block;background:#7c4dff;color:#fff;
           padding:14px 20px;border-radius:10px;font-weight:700;text-decoration:none;font-size:18px">
           Open VoiceGetter →</a>
        <div style="margin-top:10px;word-break:break-all;font-size:13px">{link}</div>
        <div style="margin-top:6px;font-size:13px">Password (if asked): <code>{password}</code></div>
      </div>"""))

tunnel, url = start_tunnel()
show(url)
step("Keep this tab open. This cell keeps running so the link keeps working.")

# 6. Keep it running
try:
    while True:
        time.sleep(30)
        if server.poll() is not None:
            step("The server stopped. Restarting it...")
            print(open(LOG).read()[-2000:])
            server = start_server()
        if tunnel.poll() is not None:
            step("The link stopped. Making a new link...")
            tunnel, url = start_tunnel()
            show(url)
except KeyboardInterrupt:
    step("Stopped watching. The server still runs until the Colab session ends.")
'''


def build() -> str:
    data, bid = bundle()
    cell_src = CELL % {"bundle": data, "bundle_id": bid}
    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": INTRO.splitlines(True)},
            {"cell_type": "code", "metadata": {"cellView": "form", "id": "start"}, "execution_count": None,
             "outputs": [], "source": cell_src.splitlines(True)},
        ],
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": [], "gpuType": "T4", "name": "VoiceGetter.ipynb"},
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    text = build()
    if "--check" in sys.argv:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("Notebook is up to date." if same else "Notebook is OUT OF DATE. Run: python tools/build_colab.py")
        sys.exit(0 if same else 1)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT} ({len(text) // 1024} KB)")
