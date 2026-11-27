# Cache the SAME bootstrap/dependency installation used by upstream _setup_script.
# The agent source, selected snapshot, task, credentials and results are staged
# separately for every task. This image contains no experiment data or secrets.
FROM python:3.12-slim
RUN python -m pip install --no-cache-dir uv==0.11.4 \
    && mkdir -p /opt/flashrsi-lite/bin \
    && ln -s /usr/local/bin/uv /opt/flashrsi-lite/bin/uv \
    && uv python install 3.12 \
    && uv venv --managed-python --python 3.12 /tmp/prewarm-venv \
    && uv pip install --python /tmp/prewarm-venv/bin/python \
       PyYAML==6.0.4 openai==2.54.0 requests==2.34.2 \
    && rm -rf /tmp/prewarm-venv
