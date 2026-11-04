@echo off
rem Start VoiceGetter on Windows. The first run installs everything.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -U pip
  rem The default Windows PyTorch build has no GPU support. Get the CUDA build if an NVIDIA GPU exists.
  where nvidia-smi >nul 2>nul && pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
python -m app.main
pause
