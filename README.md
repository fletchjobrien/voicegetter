# VoiceGetter

VoiceGetter takes a YouTube link and gives you an MP3 that has only the voice of the main speaker. It is made for long (2+ hour) gameplay videos, to get clean voice data to train a voice model.

You control it from a web page on your phone. The processing runs on a computer (your PC, or Google Colab for free).

## How it works

1. **Download.** [yt-dlp](https://github.com/yt-dlp/yt-dlp) gets the audio of the video.
2. **Remove game sound and music.** [Demucs](https://github.com/facebookresearch/demucs) (`htdemucs` model) separates the voice from the other sounds. Only the voice stem is kept.
3. **Find speech.** [Silero VAD](https://github.com/snakers4/silero-vad) finds the parts where someone talks.
4. **Identify voices.** The speech is cut into 1.5 s windows. [SpeechBrain ECAPA-TDNN](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) makes a "voice fingerprint" (speaker embedding) for each window.
5. **Find the main speaker.** The windows are grouped by voice (clustering). The largest group with a female pitch (median pitch of 165 Hz or more) is the target. If you give a reference time range, the voice in that range is the target instead.
6. **Keep only her voice.** Each window that is similar to the target voice is kept. Short pieces (less than 1 s) are removed. The kept pieces are joined, with a short silence between them, into one MP3.

The video is processed in 5-minute chunks, so a 2+ hour video does not need a lot of memory.

No accounts or API keys are necessary. The models download automatically the first time (about 400 MB).

## One-click link

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/fletchjobrien/voicegetter/blob/3d0f8c0d1cd925e781e54f3e562a55e00625bc7c/colab/VoiceGetter.ipynb)

This link opens the notebook of a fixed version (commit). After you change the code, make a new link: replace the commit ID in the link with the new commit ID.

This repository is private. The first time, Colab can ask you to **Authorize with GitHub** so that it can read the notebook. If you make the repository public, this step is not necessary.

## Option A: Google Colab (phone only, free GPU)

Use this if you do not have a computer with an NVIDIA GPU. It works from a phone.

1. Open the notebook in Colab with the **Open in Colab** link (see [One-click link](#one-click-link)).
2. Tap ▶ on the cell. If Colab says "This notebook was not authored by Google", tap **Run anyway**.
3. After about 5 minutes, the cell shows an **Open VoiceGetter** button. Tap it. The link includes the password, so you do not have to type it.

The notebook has all the app code in it. It does not need a GitHub token. It selects a T4 GPU automatically.

Keep the Colab tab open. Colab stops a session after some time. See the [Colab FAQ](https://research.google.com/colaboratory/faq.html) for the limits.

**YouTube can block Colab.** YouTube often blocks downloads from cloud servers with the message "Sign in to confirm you're not a bot". If this occurs:
- Export your YouTube cookies to a `cookies.txt` file and upload it (turn on `UPLOAD_COOKIES` in the cell, then tap ▶ again). The yt-dlp wiki tells you how: [How do I pass cookies to yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp). Use a second Google account, not your main account.
- Or upload the audio/video file on the web page instead of a link.

## Option B: Your own computer (best for many videos)

You need:
- Python 3.10 to 3.13
- An NVIDIA GPU is strongly recommended (see [speed](#speed))
- [ffmpeg](https://ffmpeg.org/download.html) (optional: a fallback copy installs with the Python packages)

Start it:
- **Windows:** double-click `run.bat`
- **Linux / macOS:** `./run.sh`

The first start installs everything into a `.venv` folder. Then the window shows two links:

```
  On this computer: http://localhost:8000
  On your phone (same Wi-Fi): http://192.168.1.23:8000
```

Open the second link on your phone. The phone must be on the same Wi-Fi. If the page does not open, let Python through the firewall of your computer.

To use it away from home, use [Tailscale](https://tailscale.com/kb/1017/install) (private network for your phone and PC), or a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/). If you make the server public, set a password first:

```bash
VOICEGETTER_PASSWORD=yourpassword ./run.sh         # Linux / macOS
set VOICEGETTER_PASSWORD=yourpassword && run.bat   # Windows
```

### Command line (no web page)

```bash
python cli.py "https://www.youtube.com/watch?v=VIDEO_ID" --target female --out output
python cli.py "https://..." --ref 12:30-13:00 --zip
```

## Using the web page

1. Paste the YouTube link (or upload a file).
2. Select **Main female voice**.
3. Tap **Get voice**. The progress bar shows each step. You can close the page and come back later.
4. When it is done, listen to the result on the page, then tap **⬇ MP3**.

### If the result has the wrong voice or other voices

- **Reference time range (best fix).** Open **Advanced**. Type a start and end time where only she talks (10 to 60 seconds is enough), for example `12:30` and `13:00`. Then the program does not guess who the main speaker is.
- **Strictness.** Increase it (0.6 to 0.7) to remove more of the other voices. Decrease it (0.4) if too much of her voice is missing.
- **Remove game sound and music.** Keep this on for gameplay. Turn it off only if the video has no music or game sound, because it is the slowest step.

### Other options

| Option | Default | What it does |
|---|---|---|
| Shortest clip | 1.0 s | Removes kept pieces that are shorter than this |
| Gap between clips | 0.3 s | Silence between the pieces in the MP3 |
| ZIP of WAV clips | off | Also gives each piece as a separate 44.1 kHz WAV file |

## Speed

Demucs is the slowest step. It is much faster on a GPU. Approximate times for a 2-hour video:

| Hardware | With music removal | Without music removal |
|---|---|---|
| NVIDIA GPU (for example Colab T4, RTX 3060) | approx. 10–30 min | approx. 5–15 min |
| CPU only | approx. 3–4 hours | approx. 30–60 min |

The GPU times are estimates, not measurements. The CPU time for music removal comes from the Demucs README: "processing time should be roughly equal to 1.5 times the duration of the track". The Demucs README also says that GPU processing needs at least 3 GB of GPU memory, and about 7 GB with the default settings ([Demucs README](https://github.com/facebookresearch/demucs)).

The server does one job at a time. Other jobs wait in a queue.

## Tips for voice-model training data

- Clean audio is more important than a large quantity. The RVC project recommends at least 10 minutes of low-noise speech ([RVC README](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)). A 2-hour stream often gives much more than that.
- Listen to some parts of the result before you train. Laughs, shouts and crosstalk with other speakers can stay in the output.
- If you need clips for a training tool, enable **ZIP of WAV clips**. The WAV files are 44.1 kHz mono, without MP3 compression.
- Only use voices that you have permission to use.

## Settings (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `VOICEGETTER_PASSWORD` | (none) | Password for the web page |
| `VOICEGETTER_COOKIES` | (none) | Path to a `cookies.txt` file for yt-dlp |
| `VOICEGETTER_DATA` | `./data` | Where jobs and results are stored |
| `VOICEGETTER_MODELS` | `./models` | Where models are stored |
| `VOICEGETTER_KEEP_HOURS` | `48` | Results older than this are deleted |
| `PORT` | `8000` | Web server port |

## Troubleshooting

- **"Sign in to confirm you're not a bot"**: see the cookies steps in Option A.
- **Download fails for YouTube**: update yt-dlp (`pip install -U "yt-dlp[default]"`). Since version 2025.11.12, yt-dlp needs a JavaScript runtime such as Deno for YouTube ([yt-dlp issue #15012](https://github.com/yt-dlp/yt-dlp/issues/15012)). `requirements.txt` installs Deno from [PyPI](https://pypi.org/project/deno/).
- **"no GPU found - this will be slow"**: on Windows, delete `.venv` and run `run.bat` again with the NVIDIA driver installed. On Colab, select a GPU runtime.
- **Out of memory on the GPU**: close other programs that use the GPU.

## Changing the code

The Colab notebook has a copy of the app code in it. After you change `app/`, `cli.py` or `requirements.txt`, build the notebook again:

```bash
python tools/build_colab.py          # rebuild colab/VoiceGetter.ipynb
python tools/build_colab.py --check  # check that the notebook is up to date
```

## Files

```
app/main.py          Web server (FastAPI) and job queue
app/pipeline.py      Audio processing
app/static/index.html  Phone web page
cli.py               Command-line version
colab/VoiceGetter.ipynb  Google Colab notebook (built by tools/build_colab.py)
run.sh / run.bat     Start scripts
```
