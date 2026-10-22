#!/usr/bin/env python3
"""Reranker service with TEI protocol (Cross-Encoder on OpenVINO, e.g., Intel iGPU) - loopback only.

    python server.py --model-dir models/mmarco-mMiniLMv2-L12 [--device GPU.0] [--port 8090]
    POST /rerank {"query": "...", "texts": ["...", ...]}  ->  [{"index": 0, "score": 0.97}, ...] (descending)
    GET  /health  ->  200 {"status": "ok", ...} only after checksum and pair self-check, otherwise 503 {"status", "reason"}

Intended as a child process of the Skirnir agent; the router forwards /rerank via the tunnel. Content is not logged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rerank_service import Limits, Reply, Service, error  # noqa: E402

LOOPBACK = ("127.0.0.1", "::1", "localhost")  # the service has no authentication - local only; the agent forwards it


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "skirnir-rerank/0.1"

        def log_message(self, fmt, *args):
            return

        def _send(self, reply: Reply):
            data = json.dumps(reply.body, ensure_ascii=False).encode()
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(service.health())
            return self._send(error(404, "not_found"))

        def do_POST(self):
            length = self.headers.get("Content-Length") or ""
            if not length.isdigit():
                self.close_connection = True  # unread body: do not reuse connection
                return self._send(error(411, "length_required"))
            if int(length) > service.limits.max_body_bytes:
                self.close_connection = True
                return self._send(error(413, "body_too_large"))
            # always read the body, even for 404: otherwise Windows resets the connection while the client is still sending
            data = self.rfile.read(int(length))
            if self.path != "/rerank":
                return self._send(error(404, "not_found"))
            return self._send(service.rerank(data))

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.path.join(HERE, "models", "mmarco-mMiniLMv2-L12"))
    ap.add_argument("--device", default="GPU.0", help="OpenVINO-Geraet, z. B. GPU.0 (iGPU), CPU, NPU")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--max-texts", type=int, default=Limits.max_texts)
    a = ap.parse_args(argv)
    if a.host not in LOOPBACK:
        print(f"skirnir-rerank: --host {a.host} ist keine Loopback-Adresse - der Dienst hat keine Anmeldung und lauscht nur lokal",
              file=sys.stderr)
        return 2
    # only here: the engine needs openvino/numpy, the HTTP layer (and its self-test in CI) does not
    import engine_openvino

    service = Service(lambda: engine_openvino.build(a.model_dir, a.device, a.max_length, batch=a.batch),
                      Limits(max_texts=a.max_texts))
    # load and check model in background: /health responds 503 "starting" meanwhile
    threading.Thread(target=service.start, name="rerank-start", daemon=True).start()
    print(f"skirnir-rerank: {a.model_dir} auf {a.device}, http://{a.host}:{a.port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(service)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
