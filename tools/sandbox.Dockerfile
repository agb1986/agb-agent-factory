# Image for the sandbox tools (tools/sandbox.py). Building it needs the network; running it never
# does: every command starts with `docker run --network none`. The image must already hold what the
# repository under test needs, since nothing can be installed at run time.
#
#   docker build -t factory-sandbox:0.1 -f tools/sandbox.Dockerfile tools
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==9.1.1 uv==0.10.10
