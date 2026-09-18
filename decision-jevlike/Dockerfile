# Jevlike-Dienst fuer Skirnir (Byte-Encoder-Variante, CPU). Bild ~1 GB wegen torch; laeuft auf dem Router-CT ohne GPU.
# Bauen:   docker build -t skirnir-jevlike .
# Starten: siehe compose.yaml (bindet nur 127.0.0.1:8081, Checkpoints als Volume, kein Netz zur Laufzeit noetig)
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 HF_HOME=/models/hf-cache
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir numpy "git+https://github.com/vinnylarouge/jevlike@94f5fd1b0b11d52bbdfdf4e0ee6aa96b568f8452"
WORKDIR /app
COPY server.py /app/server.py
VOLUME ["/models"]
EXPOSE 8081
ENTRYPOINT ["python", "/app/server.py", "--host", "0.0.0.0", "--port", "8081", "--device", "cpu"]
CMD ["--checkpoint", "/models/skirnir-router-tiny.pt", "--model-name", "skirnir-router-tiny"]
