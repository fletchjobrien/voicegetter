"""Command-line version (no web server).

    python cli.py "https://www.youtube.com/watch?v=..." [--target female] [--no-separate]
                  [--ref 12:30-13:00] [--strictness 0.5] [--zip] [--out output_folder]
"""

import argparse
import sys
import time
from pathlib import Path

from app import pipeline


def to_sec(s: str) -> float:
    sec = 0.0
    for p in s.split(":"):
        sec = sec * 60 + float(p)
    return sec


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract one speaker's voice from a YouTube video.")
    ap.add_argument("source", help="YouTube link or a local audio/video file")
    ap.add_argument("--target", choices=["female", "male", "any"], default="female")
    ap.add_argument("--no-separate", action="store_true", help="Skip music removal (faster, less clean)")
    ap.add_argument("--ref", help="Time range where only she talks, e.g. 12:30-13:00")
    ap.add_argument("--strictness", type=float, default=0.5)
    ap.add_argument("--min-clip", type=float, default=1.0)
    ap.add_argument("--gap", type=float, default=0.3)
    ap.add_argument("--zip", action="store_true", help="Also write a ZIP of WAV clips")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()

    ref_start = ref_end = None
    if a.ref:
        s, e = a.ref.split("-")
        ref_start, ref_end = to_sec(s), to_sec(e)

    opt = pipeline.Options.from_dict(dict(
        target=a.target, separate=not a.no_separate, strictness=a.strictness, min_clip_sec=a.min_clip,
        gap_sec=a.gap, ref_start=ref_start, ref_end=ref_end, make_zip=a.zip))

    last = [""]

    def progress(frac, step):
        if step != last[0]:
            last[0] = step
            print(f"{frac:6.1%}  {step}", flush=True)

    rep = pipeline.Reporter(progress=progress, log=lambda m: print("        " + m, flush=True))
    src = Path(a.source) if Path(a.source).is_file() else a.source
    t = time.time()
    res = pipeline.process(src, Path(a.out), opt, rep)
    print(f"\nDone in {(time.time() - t) / 60:.1f} min")
    print(f"MP3: {res.mp3}")
    if res.zip:
        print(f"ZIP: {res.zip}")


if __name__ == "__main__":
    sys.exit(main())
