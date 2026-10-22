#!/usr/bin/env python3
"""Self-test of the reranker service (rerank-server/) without OpenVINO: engine stub, state, request validation, TEI response,
manifest checksums, and the HTTP layer on a free loopback port.
    python test/selftest_rerank_server.py
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rerank-server")
sys.path.insert(0, ROOT)
import rerank_service as rs  # noqa: E402
import server  # noqa: E402


class FakeEngine:
    """Logit = number of query words in the text (minus 2), so the ranking is predictable."""

    def __init__(self, invert=False, nan=False):
        self.invert, self.nan = invert, nan

    def describe(self):
        return {"model": "fake", "device": "none"}

    def score(self, query, texts):
        words = set(query.lower().split())
        out = [float(len(words & set(t.lower().split())) - 2) for t in texts]
        if self.nan:
            out[0] = float("nan")
        return [-x for x in out] if self.invert else out


def ok_service(**kw):
    s = rs.Service(lambda: (FakeEngine(**kw), ""))
    s.start()
    return s


def body(**fields):
    return json.dumps(fields).encode()


class TestState(unittest.TestCase):
    def test_healthy_after_selftest(self):
        s = ok_service()
        self.assertEqual(s.state, rs.OK)
        self.assertEqual(s.health().status, 200)

    def test_inverted_model_fails_selftest(self):
        s = ok_service(invert=True)
        self.assertEqual(s.state, rs.SELFTEST_FAILED)
        self.assertTrue(s.reason.startswith("selftest_pair"))
        self.assertEqual(s.health().status, 503)
        self.assertEqual(s.rerank(body(query="a", texts=["a"])).status, 503)

    def test_nan_fails_selftest(self):
        self.assertEqual(ok_service(nan=True).state, rs.SELFTEST_FAILED)

    def test_engine_not_buildable(self):
        s = rs.Service(lambda: (None, "device_unavailable:GPU.0"))
        s.start()
        self.assertEqual((s.state, s.reason), (rs.SELFTEST_FAILED, "device_unavailable:GPU.0"))

    def test_starting_answers_503(self):
        s = rs.Service(lambda: (FakeEngine(), ""))
        self.assertEqual(s.health().body["status"], rs.STARTING)


class TestRerank(unittest.TestCase):
    def setUp(self):
        self.s = ok_service()

    def test_tei_shape_sorted_sigmoid(self):
        r = self.s.rerank(body(query="haupt stadt berlin", texts=["bananen", "haupt stadt berlin", "haupt stadt"], return_text=False))
        self.assertEqual(r.status, 200)
        self.assertEqual([x["index"] for x in r.body], [1, 2, 0])
        self.assertTrue(all(0.0 < x["score"] < 1.0 for x in r.body))
        self.assertEqual(sorted(x["index"] for x in r.body), [0, 1, 2])

    def test_rejections(self):
        cases = {
            b"kein json": (400, "invalid_json"),
            body(texts=["a"]): (400, "query_missing"),
            body(query="a", texts=[]): (400, "texts_missing"),
            body(query="a", texts=["a", 3]): (400, "texts_not_strings"),
            body(query="a", texts=["a"] * 513): (413, "too_many_texts"),
            body(query="a", texts=["x" * 9000]): (413, "text_too_long"),
        }
        for raw, (status, reason) in cases.items():
            r = self.s.rerank(raw)
            self.assertEqual((r.status, r.body["error"]["reason"]), (status, reason), raw[:40])

    def test_sigmoid_extremes(self):
        self.assertAlmostEqual(rs.sigmoid(0.0), 0.5)
        self.assertGreater(rs.sigmoid(800.0), 0.999)
        self.assertLess(rs.sigmoid(-800.0), 1e-300)


class TestManifest(unittest.TestCase):
    def test_checksums(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rs.verify_manifest(d), (None, "manifest_missing"))
            with open(os.path.join(d, "model.bin"), "wb") as fh:
                fh.write(b"gewichte")
            good = hashlib.sha256(b"gewichte").hexdigest()
            with open(os.path.join(d, "manifest.json"), "w") as fh:
                json.dump({"files": {"model.bin": good}}, fh)
            manifest, reason = rs.verify_manifest(d)
            self.assertEqual(reason, "")
            with open(os.path.join(d, "model.bin"), "wb") as fh:
                fh.write(b"gewichtX")  # same size, different content - the case from 2026-10-20
            self.assertEqual(rs.verify_manifest(d), (None, "checksum_mismatch:model.bin"))


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(ok_service()))
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, data=None):
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:  # exception-ok: urllib reports HTTP status >= 400 only this way
            return e.code, json.loads(e.read())

    def test_health_and_rerank(self):
        self.assertEqual(self.call("/health")[0], 200)
        status, answer = self.call("/rerank", body(query="haupt stadt", texts=["x", "haupt stadt"]))
        self.assertEqual((status, answer[0]["index"]), (200, 1))

    def test_unknown_path(self):
        self.assertEqual(self.call("/embed", body(inputs="x"))[0], 404)

    def test_non_loopback_host_refused(self):
        self.assertEqual(server.main(["--host", "192.0.2.10", "--model-dir", "unbenutzt"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
