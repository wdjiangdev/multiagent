FROM python:3.12-slim

WORKDIR /app

# 基础目录
RUN mkdir -p /slots /data /setup-token-dir

COPY app.py .

# 以 uid 1000 运行
RUN useradd -u 1000 opc && chown -R opc:opc /app /data /setup-token-dir

USER opc

ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py"]
