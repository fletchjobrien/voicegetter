"""Audio pipeline: YouTube link -> one speaker's voice as MP3.

Steps
1. Download the audio with yt-dlp and convert it to a 44.1 kHz WAV with ffmpeg.
2. Remove game sound and music with Demucs (keep the "vocals" stem).
3. Find speech with Silero VAD.
4. Cut the speech into short windows and make a speaker embedding
   (SpeechBrain ECAPA-TDNN) for each window.
5. Find the main speaker: cluster the embeddings, then pick the largest
   cluster (optionally only clusters with a female/male pitch range), or use
   a reference time range that the user gives.
6. Keep only the audio that matches that speaker and write it as MP3
   (and optionally as a ZIP of WAV clips).

All heavy steps work on chunks, so a 2+ hour video does not need a lot of RAM.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

SEP_SR = 44100          # Demucs sample rate, also the output sample rate
EMB_SR = 16000          # VAD and speaker-embedding sample rate
CHUNK_SEC = 300         # Process the video in 5-minute chunks
WIN_SEC = 1.5           # Speaker-embedding window length
HOP_SEC = 0.75          # Speaker-embedding window hop
MIN_WIN_SEC = 0.6       # Speech regions shorter than this are dropped
FRAME_SEC = 0.01        # Time grid for the keep/drop decision
CLUSTER_SAMPLE = 3000   # Max windows used for clustering
CLUSTER_DIST = 0.65     # Cosine distance threshold for clustering
FEMALE_MIN_HZ = 165.0   # Median pitch that splits "female" and "male"

MODEL_DIR = Path(os.environ.get("VOICEGETTER_MODELS", Path(__file__).resolve().parent.parent / "models"))


class Cancelled(Exception):
    pass


@dataclass
class Options:
    target: str = "female"          # "female", "male" or "any"
    separate: bool = True           # Remove music and game sound with Demucs
    strictness: float = 0.5         # Similarity threshold (higher = cleaner, less audio)
    min_clip_sec: float = 1.0       # Drop kept pieces shorter than this
    gap_sec: float = 0.3            # Silence between pieces in the MP3
    ref_start: float | None = None  # Optional: a time range where only she talks
    ref_end: float | None = None
    make_zip: bool = False          # Also make a ZIP of WAV clips
    mp3_bitrate: str = "192k"

    @classmethod
    def from_dict(cls, d: dict) -> "Options":
        o = cls()
        for k, v in d.items():
            if hasattr(o, k) and v is not None and v != "":
                setattr(o, k, v)
        o.strictness = float(min(max(float(o.strictness), 0.2), 0.9))
        o.min_clip_sec = max(float(o.min_clip_sec), 0.0)
        o.gap_sec = min(max(float(o.gap_sec), 0.0), 3.0)
        if o.target not in ("female", "male", "any"):
            o.target = "female"
        if o.ref_start is not None and o.ref_end is not None:
            o.ref_start, o.ref_end = float(o.ref_start), float(o.ref_end)
            if o.ref_end <= o.ref_start:
                raise ValueError("Reference end time must be after the start time.")
        else:
            o.ref_start = o.ref_end = None
        return o


@dataclass
class Reporter:
    """Sends progress to the job. `check` raises Cancelled if the user cancelled."""
    progress: Callable[[float, str], None]
    log: Callable[[str], None]
    check: Callable[[], None] = lambda: None


@dataclass
class Window:
    start: float
    end: float
    region: int


@dataclass
class Result:
    mp3: Path
    zip: Path | None
    title: str
    stats: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        raise RuntimeError("ffmpeg is not installed. Install ffmpeg and try again.") from e


def run_ffmpeg(args: list[str]) -> None:
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr.strip()[-800:]}")


def pick_device() -> str:
    import torch
    threads = os.environ.get("VOICEGETTER_THREADS")
    if threads:  # In containers, torch can see all host CPUs and use too many threads.
        torch.set_num_threads(int(threads))
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def safe_name(s: str, limit: int = 80) -> str:
    keep = "".join(c if c.isalnum() or c in " -_()" else "_" for c in s).strip()
    return (keep or "voice")[:limit].strip()


# ----------------------------------------------------------------------------
# Step 1: download
# ----------------------------------------------------------------------------

def download(url: str, workdir: Path, rep: Reporter) -> tuple[Path, str]:
    import yt_dlp

    def hook(d):
        rep.check()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                rep.progress(0.08 * done / total, f"Downloading audio ({done / total:.0%})")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "ffmpeg_location": ffmpeg_bin(),
    }
    cookies = os.environ.get("VOICEGETTER_COOKIES")
    if cookies and Path(cookies).is_file():
        opts["cookiefile"] = cookies
    # yt-dlp needs a JavaScript runtime for YouTube. Use Deno (installed by
    # requirements.txt into the venv) or Node if it is installed.
    runtimes = {}
    deno = shutil.which("deno") or shutil.which("deno", path=str(Path(sys.executable).parent))
    if deno:
        runtimes["deno"] = {"path": deno}
    node = shutil.which("node")
    if node:
        runtimes["node"] = {"path": node}
    if runtimes:
        opts["js_runtimes"] = runtimes
    else:
        rep.log("WARNING: no JavaScript runtime (Deno) found. YouTube downloads can fail.")

    rep.progress(0.0, "Getting video info")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next(e for e in info["entries"] if e)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        if isinstance(getattr(e, "exc_info", (None, None))[1], Cancelled):
            raise Cancelled() from e
        raise RuntimeError(explain_download_error(str(e))) from e
    if not path.exists():
        found = sorted(workdir.glob("source.*"))
        if not found:
            raise RuntimeError("Download failed: no audio file was saved.")
        path = found[0]
    title = info.get("title") or "voice"
    rep.log(f"Downloaded: {title} ({fmt_time(info.get('duration') or 0)})")
    return path, title


def explain_download_error(msg: str) -> str:
    """Make common yt-dlp errors easier to understand."""
    msg = msg.replace("ERROR: ", "").split("; please report this issue")[0].strip()
    low = msg.lower()
    if "confirm you" in low and "bot" in low:
        hint = ("YouTube blocked this server. Give yt-dlp your YouTube cookies (VOICEGETTER_COOKIES, "
                "see README), or upload the audio file instead of a link.")
    elif any(k in low for k in ("sign in", "age-restricted", "age restricted", "members-only", "private video")):
        hint = "This video needs a login. Use cookies (see README) or upload the file."
    elif "proxy" in low or "unable to connect" in low or "timed out" in low:
        hint = "The server cannot connect to YouTube. Check the internet connection of the server."
    elif "javascript" in low or "n challenge" in low or "signature" in low:
        hint = "Update yt-dlp (pip install -U \"yt-dlp[default]\") and make sure Deno is installed."
    else:
        hint = "Check the link. If the link is correct, update yt-dlp (pip install -U \"yt-dlp[default]\")."
    return f"Download failed. {hint}\nDetails: {msg[:400]}"


def to_wav(src: Path, dst: Path, rep: Reporter) -> float:
    rep.progress(0.08, "Converting audio")
    run_ffmpeg(["-i", str(src), "-vn", "-ac", "2", "-ar", str(SEP_SR), "-c:a", "pcm_s16le", str(dst)])
    return sf.info(str(dst)).duration


# ----------------------------------------------------------------------------
# Models (loaded once per process)
# ----------------------------------------------------------------------------

_models: dict = {}


def get_demucs(device: str):
    if "demucs" not in _models:
        import torch
        from demucs.pretrained import get_model
        torch.hub.set_dir(str(MODEL_DIR / "torch_hub"))
        m = get_model("htdemucs")
        m.to(device).eval()
        _models["demucs"] = m
    return _models["demucs"]


def get_vad():
    if "vad" not in _models:
        from silero_vad import load_silero_vad
        _models["vad"] = load_silero_vad()
    return _models["vad"]


def get_encoder(device: str):
    if "ecapa" not in _models:
        from speechbrain.inference.speaker import EncoderClassifier
        kwargs = {}
        try:  # Copy files instead of symlinks (symlinks fail on Windows without admin)
            from speechbrain.utils.fetching import LocalStrategy
            kwargs["local_strategy"] = LocalStrategy.COPY
        except Exception:
            pass
        _models["ecapa"] = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(MODEL_DIR / "spkrec-ecapa-voxceleb"),
            run_opts={"device": device},
            **kwargs,
        )
    return _models["ecapa"]


# ----------------------------------------------------------------------------
# Steps 2-4: separate, VAD, embeddings (chunk by chunk)
# ----------------------------------------------------------------------------

def separate_chunk(stereo: np.ndarray, device: str) -> np.ndarray:
    """stereo: (samples, 2) float32 at 44.1 kHz -> mono vocals (samples,)."""
    import torch
    from demucs.apply import apply_model

    model = get_demucs(device)
    wav = torch.from_numpy(stereo.T.copy())  # (2, T)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav = (wav - mean) / std
    with torch.no_grad():
        out = apply_model(model, wav[None], device=device, shifts=1, split=True, overlap=0.25, progress=False)[0]
    out = out * std + mean
    vocals = out[model.sources.index("vocals")]
    return vocals.mean(0).cpu().numpy().astype(np.float32)


def resample_to_16k(x: np.ndarray) -> np.ndarray:
    from scipy.signal import resample_poly
    return resample_poly(x, 160, 441).astype(np.float32)


def speech_regions(x16: np.ndarray) -> list[tuple[float, float]]:
    import torch
    from silero_vad import get_speech_timestamps

    ts = get_speech_timestamps(
        torch.from_numpy(x16), get_vad(), sampling_rate=EMB_SR,
        threshold=0.5, min_speech_duration_ms=300, min_silence_duration_ms=300, speech_pad_ms=60,
    )
    return [(t["start"] / EMB_SR, t["end"] / EMB_SR) for t in ts]


def make_windows(start: float, end: float, region: int) -> list[Window]:
    length = end - start
    if length < MIN_WIN_SEC:
        return []
    if length <= WIN_SEC:
        return [Window(start, end, region)]
    n = int(math.ceil((length - WIN_SEC) / HOP_SEC)) + 1
    wins = []
    for i in range(n):
        s = min(start + i * HOP_SEC, end - WIN_SEC)
        wins.append(Window(s, s + WIN_SEC, region))
    return wins


def embed(pieces: list[np.ndarray], device: str, batch: int = 64) -> np.ndarray:
    import torch

    enc = get_encoder(device)
    out = []
    for i in range(0, len(pieces), batch):
        group = pieces[i:i + batch]
        longest = max(len(p) for p in group)
        wavs = torch.zeros(len(group), longest)
        lens = torch.zeros(len(group))
        for j, p in enumerate(group):
            wavs[j, :len(p)] = torch.from_numpy(p)
            lens[j] = len(p) / longest
        with torch.no_grad():
            e = enc.encode_batch(wavs.to(device), lens.to(device)).squeeze(1)
        out.append(e.cpu().numpy())
    emb = np.concatenate(out).astype(np.float32)
    return emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-8)


# ----------------------------------------------------------------------------
# Step 5: choose the speaker
# ----------------------------------------------------------------------------

def median_pitch(pieces: list[np.ndarray]) -> float:
    import librosa
    if not pieces:
        return float("nan")
    x = np.concatenate(pieces)
    f0, voiced, _ = librosa.pyin(x, fmin=70, fmax=500, sr=EMB_SR, frame_length=1024)
    f0 = f0[voiced & ~np.isnan(f0)]
    return float(np.median(f0)) if len(f0) else float("nan")


def choose_centroid(emb: np.ndarray, wins: list[Window], audio16: Callable[[Window], np.ndarray],
                    opt: Options, rep: Reporter) -> tuple[np.ndarray, dict]:
    info: dict = {}
    if opt.ref_start is not None:
        sel = [i for i, w in enumerate(wins)
               if min(w.end, opt.ref_end) - max(w.start, opt.ref_start) >= 0.5 * (w.end - w.start)]
        if not sel:
            raise RuntimeError("No speech found in the reference time range. Pick a range where she talks.")
        c = emb[sel].mean(0)
        info["mode"] = f"reference {fmt_time(opt.ref_start)}-{fmt_time(opt.ref_end)} ({len(sel)} windows)"
        rep.log(f"Using reference range: {len(sel)} speech windows")
        return c / np.linalg.norm(c), info

    from sklearn.cluster import AgglomerativeClustering

    rng = np.random.default_rng(0)
    idx = np.arange(len(wins))
    if len(idx) > CLUSTER_SAMPLE:
        idx = np.sort(rng.choice(idx, CLUSTER_SAMPLE, replace=False))
    if len(idx) < 2:
        c = emb.mean(0)
        return c / np.linalg.norm(c), {"mode": "single window"}

    labels = AgglomerativeClustering(
        n_clusters=None, metric="cosine", linkage="average", distance_threshold=CLUSTER_DIST,
    ).fit_predict(emb[idx])

    clusters = sorted(((np.mean(labels == lab), idx[labels == lab]) for lab in np.unique(labels)),
                      key=lambda t: -t[0])
    # Ignore tiny groups (short noises, one-off voices); keep at most 6.
    clusters = [clusters[0]] + [c for c in clusters[1:6] if c[0] >= 0.03]

    rows = []
    for share, members in clusters:
        rep.check()
        sample = rng.choice(members, min(len(members), 40), replace=False)
        pitch = median_pitch([audio16(wins[i]) for i in sample])
        rows.append((share, members, pitch))
        rep.log(f"Voice group: {share:.0%} of speech, median pitch {pitch:.0f} Hz")

    def fits(p):
        if opt.target == "any" or math.isnan(p):
            return opt.target == "any"
        return p >= FEMALE_MIN_HZ if opt.target == "female" else p < FEMALE_MIN_HZ

    pick = next((r for r in rows if fits(r[2])), None)
    if pick is None:
        pick = rows[0]
        rep.log(f"WARNING: no {opt.target} voice group found. Using the largest voice group.")
        info["warning"] = f"No {opt.target} voice found; used the largest voice."
    share, members, pitch = pick
    c = emb[members].mean(0)
    info.update({"mode": "auto", "speaker_share": round(float(share), 3), "speaker_pitch_hz": round(float(pitch), 1),
                 "voice_groups": len(rows)})
    return c / np.linalg.norm(c), info


def keep_segments(emb: np.ndarray, wins: list[Window], regions: list[tuple[float, float]],
                  centroid: np.ndarray, opt: Options) -> list[tuple[float, float]]:
    # Refine the centroid with the windows that match well, then score again.
    scores = emb @ centroid
    good = scores >= max(opt.strictness, 0.4)
    if good.sum() >= 5:
        c = emb[good].mean(0)
        centroid = c / np.linalg.norm(c)
        scores = emb @ centroid

    by_region: dict[int, list[int]] = {}
    for i, w in enumerate(wins):
        by_region.setdefault(w.region, []).append(i)

    segs: list[tuple[float, float]] = []
    for r, ids in by_region.items():
        rs, re_ = regions[r]
        n = max(1, int(round((re_ - rs) / FRAME_SEC)))
        total = np.zeros(n)
        count = np.zeros(n)
        for i in ids:
            a = int((wins[i].start - rs) / FRAME_SEC)
            b = max(a + 1, int(math.ceil((wins[i].end - rs) / FRAME_SEC)))
            total[a:b] += scores[i]
            count[a:b] += 1
        mean = np.where(count > 0, total / np.maximum(count, 1), -1.0)
        keep = mean >= opt.strictness
        # Runs of kept frames -> segments
        edges = np.flatnonzero(np.diff(np.concatenate([[0], keep.astype(np.int8), [0]])))
        for a, b in zip(edges[::2], edges[1::2]):
            s, e = rs + a * FRAME_SEC, rs + b * FRAME_SEC
            if e - s >= opt.min_clip_sec:
                segs.append((s, e))
    segs.sort()
    return segs


# ----------------------------------------------------------------------------
# Step 6: write output
# ----------------------------------------------------------------------------

def write_output(vocals: Path, segs: list[tuple[float, float]], workdir: Path, title: str,
                 opt: Options, rep: Reporter) -> tuple[Path, Path | None]:
    name = safe_name(title)
    wav_out = workdir / "voice.wav"
    mp3_out = workdir / f"{name} - voice.mp3"
    zip_out = workdir / f"{name} - clips.zip" if opt.make_zip else None
    fade = int(0.01 * SEP_SR)
    ramp = np.linspace(0, 1, fade, dtype=np.float32)
    gap = np.zeros(int(opt.gap_sec * SEP_SR), dtype=np.float32)

    zf = zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) if zip_out else None
    try:
        with sf.SoundFile(str(vocals)) as src, sf.SoundFile(str(wav_out), "w", SEP_SR, 1, "PCM_16") as dst:
            for k, (s, e) in enumerate(segs):
                if k % 50 == 0:
                    rep.check()
                    rep.progress(0.92 + 0.05 * k / max(len(segs), 1), "Writing audio")
                src.seek(int(s * SEP_SR))
                x = src.read(int((e - s) * SEP_SR), dtype="float32")
                if len(x) > 2 * fade:
                    x[:fade] *= ramp
                    x[-fade:] *= ramp[::-1]
                dst.write(x)
                if len(gap):
                    dst.write(gap)
                if zf:
                    clip = workdir / "clip.wav"
                    sf.write(str(clip), x, SEP_SR, subtype="PCM_16")
                    zf.write(clip, f"{name}_{k + 1:05d}_{int(s)}s.wav")
                    clip.unlink()
    finally:
        if zf:
            zf.close()
    rep.progress(0.97, "Making MP3")
    run_ffmpeg(["-i", str(wav_out), "-c:a", "libmp3lame", "-b:a", opt.mp3_bitrate, str(mp3_out)])
    wav_out.unlink(missing_ok=True)
    return mp3_out, zip_out


# ----------------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------------

def process(source: str | Path, workdir: Path, opt: Options, rep: Reporter, title: str | None = None) -> Result:
    """`source` is a URL, or a path to an audio/video file that is already on disk."""
    workdir.mkdir(parents=True, exist_ok=True)
    device = pick_device()
    rep.log(f"Device: {device}" + ("" if device == "cuda" else " (no GPU found - this will be slow)"))

    if isinstance(source, Path) or Path(str(source)).is_file():
        src_path, title = Path(source), title or Path(source).stem
    else:
        src_path, title = download(str(source), workdir, rep)
    rep.check()

    mix = workdir / "mix.wav"
    duration = to_wav(src_path, mix, rep)
    if src_path.parent == workdir:
        src_path.unlink(missing_ok=True)
    rep.log(f"Audio length: {fmt_time(duration)}")

    vocals = workdir / "vocals.wav"
    regions: list[tuple[float, float]] = []
    wins: list[Window] = []
    embs: list[np.ndarray] = []
    n_chunks = max(1, int(math.ceil(duration / CHUNK_SEC)))
    chunk_frames = CHUNK_SEC * SEP_SR

    with sf.SoundFile(str(mix)) as f, sf.SoundFile(str(vocals), "w", SEP_SR, 1, "PCM_16") as vout:
        for ci in range(n_chunks):
            rep.check()
            t0 = ci * CHUNK_SEC
            frac = 0.1 + 0.8 * ci / n_chunks
            label = f"chunk {ci + 1}/{n_chunks} ({fmt_time(t0)})"
            stereo = f.read(chunk_frames, dtype="float32", always_2d=True)
            if stereo.shape[1] == 1:
                stereo = np.repeat(stereo, 2, axis=1)
            if opt.separate and len(stereo) >= SEP_SR:  # Demucs needs at least ~1 s
                rep.progress(frac, f"Removing music and game sound: {label}")
                mono = separate_chunk(stereo, device)
            else:
                mono = stereo.mean(1)
            vout.write(np.clip(mono, -1, 1))

            rep.progress(frac + 0.6 / n_chunks, f"Finding speech: {label}")
            x16 = resample_to_16k(mono)
            pieces = []
            for rs, re_ in speech_regions(x16):
                r = len(regions)
                regions.append((t0 + rs, t0 + re_))
                for w in make_windows(t0 + rs, t0 + re_, r):
                    wins.append(w)
                    a, b = int((w.start - t0) * EMB_SR), int((w.end - t0) * EMB_SR)
                    pieces.append(x16[a:b])
            if pieces:
                rep.progress(frac + 0.7 / n_chunks, f"Analysing voices: {label}")
                embs.append(embed(pieces, device))
    mix.unlink(missing_ok=True)

    speech_sec = sum(e - s for s, e in regions)
    rep.log(f"Speech found: {fmt_time(speech_sec)} in {len(regions)} parts")
    if not wins:
        raise RuntimeError("No speech was found in this video.")
    emb = np.concatenate(embs)

    rep.progress(0.9, "Finding the main speaker")
    vfile = sf.SoundFile(str(vocals))

    def audio16(w: Window) -> np.ndarray:
        vfile.seek(int(w.start * SEP_SR))
        return resample_to_16k(vfile.read(int((w.end - w.start) * SEP_SR), dtype="float32"))

    try:
        centroid, info = choose_centroid(emb, wins, audio16, opt, rep)
    finally:
        vfile.close()

    segs = keep_segments(emb, wins, regions, centroid, opt)
    kept = sum(e - s for s, e in segs)
    rep.log(f"Kept {fmt_time(kept)} of the target voice in {len(segs)} clips")
    if not segs:
        raise RuntimeError("No clips matched the speaker. Try a lower strictness or a reference time range.")

    mp3, zp = write_output(vocals, segs, workdir, title, opt, rep)
    vocals.unlink(missing_ok=True)
    stats = {
        "title": title,
        "duration_sec": round(duration, 1),
        "speech_sec": round(speech_sec, 1),
        "kept_sec": round(float(kept), 1),
        "clips": len(segs),
        **info,
    }
    (workdir / "segments.json").write_text(json.dumps({"stats": stats, "segments": segs}, indent=1))
    rep.progress(1.0, "Done")
    return Result(mp3=mp3, zip=zp, title=title, stats=stats)
