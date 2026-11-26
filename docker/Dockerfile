# Build once while online; task execution is network-disabled.
FROM python:3.12.12-slim
RUN pip install --no-cache-dir pytest==8.4.2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace
WORKDIR /workspace
# Target repositories may need an image with additional pinned dependencies.
USER 65534:65534
