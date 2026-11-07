FROM python:3.12-slim
RUN pip install --no-cache-dir nanobot-ai==0.4.1
COPY runner.py history.py /app/
RUN chmod 0555 /app && chmod 0444 /app/runner.py /app/history.py
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 65534:65534
ENTRYPOINT ["python", "-u", "/app/runner.py"]
