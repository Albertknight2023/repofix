FROM python:3.11-slim

RUN python -m pip install --no-cache-dir "pytest>=8,<10"
COPY src/repofix/__init__.py /opt/repofix/repofix/__init__.py
COPY src/repofix/execution/__init__.py /opt/repofix/repofix/execution/__init__.py
COPY src/repofix/execution/pytest_capture.py /opt/repofix/repofix/execution/pytest_capture.py

ENV PYTHONPATH=/opt/repofix \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1
WORKDIR /workspace/repo

