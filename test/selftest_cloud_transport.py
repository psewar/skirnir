"""Provider transport policy at config load and at every actual HTTP dispatch."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))
from skirnir_router import cloud, config, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402


class ProviderURLs(unittest.TestCase):
    def test_config_load(self):
        data = yaml.safe_load((ROOT / "router/config.example.yaml").read_text())
        provider = {"kind": "openai", "api_key_env": "SYNTHETIC_KEY"}
        # keep the example's own hosts (its providers must stay valid), add the synthetic ones
        data["router"]["cloud"]["egress_allow"] = [*data["router"]["cloud"].get("egress_allow", []),
                                                   "model.example.test", "127.0.0.1", "::1"]
        data["router"]["cloud"]["providers"]["demo"] = provider
        with tempfile.TemporaryDirectory() as td:
            for n, (url, valid) in enumerate([
                ("https://model.example.test/v1", True), ("http://127.0.0.1:9000/v1", True),
                ("http://[::1]:9000/v1", True), ("http://model.example.test/v1", False),
                ("https://unknown.example.net", False), ("ftp://127.0.0.1", False),
                ("https://model.example.test/v1?key=value", False), ("https://model.example.test/#fragment", False),
                ("https://user@model.example.test", False), ("https://model.example.test:invalid", False)
            ]):
                with self.subTest(url=url):
                    provider["base_url"] = url
                    path = Path(td) / f"{n}.yaml"
                    path.write_text(yaml.safe_dump(data))
                    result = config.Config.load(str(path), use_overrides=False)
                    self.assertEqual(not failed(result), valid, getattr(result, "message", ""))


class ProviderRequests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_never_reaches_second_origin(self):
        hits = []
        status = 307

        async def sink(request):
            hits.append(request.method)
            return web.Response()

        dest = web.Application()
        dest.router.add_route("*", "/sink", sink)
        async with TestServer(dest) as receiver:
            async def redirect(request):
                return web.Response(status=status, headers={"Location": str(receiver.make_url("/sink"))})

            src = web.Application()
            src.router.add_post("/{rest:.*}", redirect)
            async with TestServer(src) as provider, aiohttp.ClientSession() as session:
                state.SESSION = session
                state.CFG = SimpleNamespace(models={}, request_timeout_s=5)
                for status in (300, 301, 302, 303, 307, 308):
                    for kind, path, stream in [
                        ("openai", "/api/chat", False), ("openai", "/api/chat", True),
                        ("openai", "/api/embed", False), ("anthropic", "/api/chat", False),
                        ("anthropic", "/api/chat", True)
                    ]:
                        with self.subTest(status=status, kind=kind, path=path, stream=stream):
                            target = SimpleNamespace(spec={"kind": kind, "base_url": str(provider.make_url("/"))},
                                                     provider="fixture", api_key="synthetic-key")
                            body = {"messages": [{"role": "user", "content": "synthetic"}], "input": "synthetic"}
                            with self.assertRaises(cloud.CloudError) as error:
                                _, chunks = await cloud.run(target, path, body, "fixture", stream)
                                if stream:
                                    async for _ in chunks:
                                        pass
                            self.assertEqual((error.exception.status, error.exception.code), (502, "provider_redirect"))
                            self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
