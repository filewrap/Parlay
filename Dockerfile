FROM python:3.11-slim

# ffmpeg is required for audio decode/resample; git for any VCS installs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# Session file and .env are provided via a mounted volume at runtime.
CMD ["python", "-m", "parlay"]
