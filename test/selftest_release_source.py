#!/usr/bin/env python3
"""Self-test (Router 0.41.0, ADR-0117/ADR-0124): the router's Ollama version check through a replaceable release source.

`github` (default) reads the GitHub API's release description as before; `document` reads a neutral release document
{version, files: {"<os>/<arch>": {url, sha256, size}}}. For the same release both must yield the same LATEST entry, so the
order to the agent is identical. Rejected: a redirect (never followed), an oversized or malformed document, plain HTTP to a
non-loopback host, a timeout. Runs against loopback test servers only."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))
from skirnir_router import config, ollamaupdate, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

VERSION = "0.35.1"
SUMS = {"ollama-windows-amd64.zip": "a" * 64, "ollama-linux-amd64.tar.zst": "B" * 64}
SIZES = {"ollama-windows-amd64.zip": 1_900_000_000, "ollama-linux-amd64.tar.zst": 1_700_000_000}


class Fixture:
    """One loopback server playing GitHub (release JSON, sha256sum.txt), a document host and a few misbehaving hosts."""

    def __init__(self):
        self.hits = []
        self.document = None
        self.raw = None
        self.status = 200

    def asset_url(self, server, name):
        return str(server.make_url(f"/download/v{VERSION}/{name}"))

    async def start(self):
        app = web.Application()
        app.router.add_get("/repos/ollama/ollama/releases/latest", self.github)
        app.router.add_get(f"/download/v{VERSION}/sha256sum.txt", self.sums)
        app.router.add_get("/release.json", self.doc)
        app.router.add_get("/moved.json", self.moved)
        app.router.add_get("/slow.json", self.slow)
        app.router.add_get("/huge-chunked.json", self.huge_chunked)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return self.server

    async def github(self, request):
        self.hits.append(request.path)
        assets = [{"name": n, "size": SIZES[n], "browser_download_url": self.asset_url(self.server, n)} for n in SUMS]
        assets.append({"name": "sha256sum.txt", "browser_download_url": self.asset_url(self.server, "sha256sum.txt")})
        return web.json_response({"tag_name": f"v{VERSION}", "assets": assets})

    async def sums(self, request):
        self.hits.append(request.path)
        return web.Response(text="".join(f"{h.lower()}  ./{n}\n" for n, h in SUMS.items()) + "0" * 64 + "  ./install.sh\n")

    async def doc(self, request):
        self.hits.append(request.path)
        if self.raw is not None:
            return web.Response(body=self.raw, status=self.status, content_type="application/json")
        return web.json_response(self.document, status=self.status)

    async def moved(self, request):
        self.hits.append(request.path)
        return web.Response(status=302, headers={"Location": str(self.server.make_url("/release.json"))})

    async def slow(self, request):
        await asyncio.sleep(3)
        return web.json_response(self.document)

    async def huge_chunked(self, request):
        response = web.StreamResponse()
        response.enable_chunked_encoding()
        await response.prepare(request)
        for _ in range(20):
            await response.write(b" " * 8192)
        await response.write_eof()
        return response


def document_for(fixture):
    return {"version": VERSION, "files": {
        "windows/amd64": {"url": fixture.asset_url(fixture.server, "ollama-windows-amd64.zip"),
                          "sha256": SUMS["ollama-windows-amd64.zip"], "size": SIZES["ollama-windows-amd64.zip"]},
        "linux/amd64": {"url": fixture.asset_url(fixture.server, "ollama-linux-amd64.tar.zst"),
                        "sha256": SUMS["ollama-linux-amd64.tar.zst"], "size": SIZES["ollama-linux-amd64.tar.zst"]},
        "darwin/arm64": {"url": "https://mirror.example.net/ollama-darwin.tgz", "sha256": "c" * 64, "size": 1},
    }}


class ReleaseSources(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx = Fixture()
        await self.fx.start()
        self.addAsyncCleanup(self.fx.server.close)
        self.fx.document = document_for(self.fx)

    async def test_document_matches_github(self):
        gh = await ollamaupdate._fetch_release(str(self.fx.server.make_url("/repos/ollama/ollama/releases/latest")))
        doc = await ollamaupdate._fetch_document(str(self.fx.server.make_url("/release.json")))
        self.assertFalse(failed(gh), getattr(gh, "message", ""))
        self.assertFalse(failed(doc), getattr(doc, "message", ""))
        self.assertEqual(doc, gh, "same release -> same LATEST entry -> same order to the agent")
        self.assertEqual(sorted(doc[1]), ["linux/amd64", "windows/amd64"], "platforms without an agent archive are skipped")
        self.assertEqual(doc[1]["linux/amd64"]["sha256"], "b" * 64, "hashes are normalised to lower case like sha256sum.txt")

    async def test_check_latest_uses_the_configured_source(self):
        state.CFG = SimpleNamespace(ollama_update={"release_url": str(self.fx.server.make_url("/release.json")),
                                                   "release_source": "document"})
        saved = ollamaupdate.save_latest
        ollamaupdate.save_latest = lambda: None
        try:
            latest = await ollamaupdate.check_latest()
        finally:
            ollamaupdate.save_latest = saved
        self.assertEqual((latest["version"], latest["error"]), (VERSION, None))
        self.assertEqual(self.fx.hits, ["/release.json"], "document source: no GitHub call")

    async def rejected(self, path="/release.json"):
        result = await ollamaupdate._fetch_document(str(self.fx.server.make_url(path)))
        self.assertTrue(failed(result), str(result))
        self.assertEqual(result.status, 502)
        return result

    async def test_redirect_is_not_followed(self):
        problem = await self.rejected("/moved.json")
        self.assertIn("redirect", problem.message)
        self.assertEqual(self.fx.hits, ["/moved.json"], "the redirect target is never fetched")

    async def test_oversized(self):
        self.fx.raw = b"{" + b" " * ollamaupdate.DOCUMENT_MAX_BYTES + b"}"
        self.assertIn("larger", (await self.rejected()).message)
        self.assertIn("larger", (await self.rejected("/huge-chunked.json")).message, "also without Content-Length")

    async def test_timeout(self):
        saved = ollamaupdate.DOCUMENT_TIMEOUT_S
        ollamaupdate.DOCUMENT_TIMEOUT_S = 0.5
        try:
            self.assertIn("Timeout", (await self.rejected("/slow.json")).message)
        finally:
            ollamaupdate.DOCUMENT_TIMEOUT_S = saved

    async def test_http_status_and_invalid_json(self):
        self.fx.status = 404
        await self.rejected()
        self.fx.status, self.fx.raw = 200, b"not json"
        await self.rejected()
        self.fx.raw = b"\xff\xfe"
        await self.rejected()
        self.fx.raw = b"[" * 30000 + b"]" * 30000   # under the size limit, but nested deeper than the JSON parser recurses
        await self.rejected()

    async def test_plain_http_to_non_loopback(self):
        result = await ollamaupdate._fetch_document("http://mirror.example.net/release.json")
        self.assertTrue(failed(result))
        self.assertIn("HTTPS", result.message)


class DocumentShape(unittest.TestCase):
    def good(self):
        return {"version": VERSION, "files": {"windows/amd64": {
            "url": "https://mirror.example.net/v0.35.1/ollama-windows-amd64.zip", "sha256": "d" * 64, "size": 10}}}

    def test_accepted(self):
        version, files = ollamaupdate.parse_document(self.good())
        self.assertEqual((version, files["windows/amd64"]["name"]), (VERSION, "ollama-windows-amd64.zip"))
        doc = self.good()
        doc["version"] = "0.36.0-rc1"
        del doc["files"]["windows/amd64"]["size"]
        version, files = ollamaupdate.parse_document(doc)
        self.assertEqual((version, files["windows/amd64"]["size"]), ("0.36.0-rc1", 0))

    def test_rejected(self):
        def with_entry(**changes):
            doc = self.good()
            doc["files"]["windows/amd64"].update(changes)
            return doc
        many = self.good()
        many["files"].update({f"os{i}/arch": {} for i in range(ollamaupdate.DOCUMENT_MAX_FILES)})
        for label, doc in [
            ("not an object", ["x"]), ("no files", {"version": VERSION}), ("files not a map", {"version": VERSION, "files": []}),
            ("version not a version", {**self.good(), "version": "latest"}),
            ("version with a path", {**self.good(), "version": "0.1.0/../../x"}),
            ("version a number", {**self.good(), "version": 0.35}),
            ("no known platform", {"version": VERSION, "files": {"darwin/arm64": {}}}),
            ("platform key malformed", {"version": VERSION, "files": {"../windows": {}}}),
            ("too many files", many),
            ("sha256 short", with_entry(sha256="abc")), ("sha256 not hex", with_entry(sha256="z" * 64)),
            ("sha256 missing", with_entry(sha256=None)),
            ("size negative", with_entry(size=-1)), ("size boolean", with_entry(size=True)),
            ("size text", with_entry(size="10")), ("size absurd", with_entry(size=ollamaupdate.DOCUMENT_MAX_SIZE + 1)),
            ("plain http url", with_entry(url="http://mirror.example.net/v0.35.1/ollama-windows-amd64.zip")),
            ("url with query", with_entry(url="https://mirror.example.net/ollama-windows-amd64.zip?sig=x")),
            ("url names another archive", with_entry(url="https://mirror.example.net/v0.35.1/other.zip")),
            ("unknown entry key", with_entry(exec="x")), ("entry not a map", {"version": VERSION, "files": {"windows/amd64": "x"}}),
        ]:
            with self.subTest(label):
                result = ollamaupdate.parse_document(doc)
                self.assertTrue(failed(result), f"{label}: {result}")


class Configuration(unittest.TestCase):
    def test_release_source_at_load(self):
        data = yaml.safe_load((ROOT / "router/config.example.yaml").read_text(encoding="utf-8"))
        ou = data["modes"]["ollama_update"]
        with tempfile.TemporaryDirectory() as td:
            for n, (source, url, valid) in enumerate([
                (None, "https://api.github.com/repos/ollama/ollama/releases/latest", True),
                ("github", "https://api.github.com/repos/ollama/ollama/releases/latest", True),
                ("document", "https://mirror.example.net/ollama/release.json", True),
                ("document", "http://127.0.0.1:8080/release.json", True),
                ("document", "http://mirror.example.net/ollama/release.json", False),
                ("document", "https://mirror.example.net/release.json?token=x", False),
                ("document", "", False),
                ("gitlab", "https://mirror.example.net/release.json", False),
            ]):
                with self.subTest(source=source, url=url):
                    if source is None:
                        ou.pop("release_source", None)
                    else:
                        ou["release_source"] = source
                    ou["release_url"] = url
                    path = Path(td) / f"{n}.yaml"
                    path.write_text(yaml.safe_dump(data), encoding="utf-8")
                    result = config.Config.load(str(path), use_overrides=False)
                    self.assertEqual(not failed(result), valid, getattr(result, "message", ""))
                    if valid:
                        self.assertEqual(result.ollama_update["release_source"], source or "github")


if __name__ == "__main__":
    unittest.main(verbosity=1)
