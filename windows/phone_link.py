"""Write an HTML page with the phone links (and QR codes) for VoiceGetter, then open it.

    python phone_link.py OUTPUT.html [--port 8000] [--tailscale NAME.ts.net]
"""

import argparse
import html
import io
import socket
import webbrowser
from pathlib import Path


def lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # No packet is sent.
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    return svg[svg.index("<svg"):]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tailscale", default="")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    links = []
    if a.tailscale:
        links.append(("Anywhere (phone needs the Tailscale app, signed in)", f"http://{a.tailscale}:{a.port}"))
    ip = lan_ip()
    if ip:
        links.append(("At home (phone on the same Wi-Fi)", f"http://{ip}:{a.port}"))
    links.append(("On this PC", f"http://localhost:{a.port}"))

    cards = []
    for title, url in links:
        qr = qr_svg(url) if "localhost" not in url else ""
        cards.append(f"""
      <section>
        <h2>{html.escape(title)}</h2>
        <a class="link" href="{html.escape(url)}">{html.escape(url)}</a>
        {f'<div class="qr">{qr}</div><p class="hint">Point your phone camera at the code, then tap the link.</p>' if qr else ''}
      </section>""")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VoiceGetter phone link</title>
<style>
  :root {{ --bg:#f6f4f9; --card:#fff; --text:#1d1a24; --muted:#6b6578; --accent:#7c4dff; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#16131c; --card:#211d29; --text:#f1eef6; --muted:#a39cb2; --accent:#a585ff; }} }}
  body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--text); font:16px/1.5 system-ui, sans-serif; }}
  main {{ max-width:640px; margin:0 auto; }}
  section {{ background:var(--card); border-radius:14px; padding:18px; margin:14px 0; }}
  h1 {{ margin:0 0 4px; }} h2 {{ font-size:1.05rem; margin:0 0 8px; }}
  .link {{ font-size:1.25rem; font-weight:700; color:var(--accent); word-break:break-all; }}
  .qr svg {{ width:220px; height:220px; background:#fff; border-radius:8px; margin-top:12px; display:block; }}
  .hint, .muted {{ color:var(--muted); font-size:.9rem; }}
</style></head><body><main>
  <h1>🎙️ VoiceGetter is running</h1>
  <p class="muted">It starts by itself when Windows starts. Keep this PC on (plugged in) to use it from your phone.</p>
  {''.join(cards)}
  <p class="muted">This page is saved on your desktop as "VoiceGetter phone link".</p>
</main></body></html>
"""
    out = Path(a.out)
    out.write_text(page, encoding="utf-8")
    print("Phone links:")
    for title, url in links:
        print(f"  {title}: {url}")
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
