FROM python:3.12-slim

WORKDIR /app

COPY relay.py /app/relay.py

RUN useradd -u 1000 -m appuser && chmod 644 /app/relay.py

USER appuser

ENTRYPOINT ["python", "/app/relay.py"]
