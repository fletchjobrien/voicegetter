# VoiceGetter container image (CPU). Used by the Cloudflare Container (cloudflare/) and works with plain Docker:
#   docker build -t voicegetter . && docker run -p 8000:8000 -e VOICEGETTER_PASSWORD=secret voicegetter
# mirror.gcr.io is Google's copy of Docker Hub images (no Docker Hub pull rate limit).
ARG BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOICEGETTER_DATA=/data \
    VOICEGETTER_MODELS=/models \
    VOICEGETTER_THREADS=4 \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    PORT=8000

WORKDIR /srv

# CPU-only PyTorch is much smaller than the default CUDA build. Set TORCH_INDEX="" to use PyPI instead.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN if [ -n "$TORCH_INDEX" ]; then \
      pip install --index-url "$TORCH_INDEX" torch==2.8.0 torchaudio==2.8.0; \
    else \
      pip install torch==2.8.0 torchaudio==2.8.0; \
    fi

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY cli.py .

# Download the AI models into the image, so jobs do not wait for downloads (about 400 MB).
ARG PREFETCH_MODELS=1
RUN if [ "$PREFETCH_MODELS" = "1" ]; then \
      python -c "from app import pipeline as P; P.get_demucs('cpu'); P.get_encoder('cpu'); P.get_vad(); print('models ok')"; \
    fi

EXPOSE 8000
CMD ["python", "-m", "app.main"]
