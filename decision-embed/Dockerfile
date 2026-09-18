# Stufe 2 fuer den Router-CT: ONNX Runtime auf CPU, kein torch. Bild ~350 MB; Modell + Kopf kommen als Volume (/models).
# Bauen:   docker build -t skirnir-embed .
# Starten: docker compose up -d   (compose.yaml, bindet nur 127.0.0.1:8082)
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4
RUN pip install --no-cache-dir numpy onnxruntime tokenizers
WORKDIR /app
COPY server.py embedder.py /app/
VOLUME ["/models"]
EXPOSE 8082
ENTRYPOINT ["python", "/app/server.py", "--host", "0.0.0.0", "--port", "8082"]
CMD ["--model-dir", "/models/e5-small", "--threads", "4"]
