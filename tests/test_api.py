import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from core.config import Settings
from core.main import create_app


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), mode="external", runtime_dir=Path(self.temp.name),
                                queue_timeout=0.02)
        self.calls = []
        self.handler = lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "Hola"}}]})

        async def upstream(req):
            self.calls.append(req)
            result = self.handler(req)
            return await result if asyncio.iscoroutine(result) else result

        self.app = create_app(self.settings, httpx.MockTransport(upstream))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.payload = {"messages": [{"role": "user", "content": "Hola"}]}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None, None, None)
        self.temp.cleanup()

    async def test_health_and_models_do_not_wake_engine(self):
        health = await self.client.get("/health")
        models = await self.client.get("/v1/models")
        self.assertEqual(health.json()["service"], "local-ia")
        self.assertEqual(models.json()["data"][0]["id"], self.settings.model)
        self.assertEqual(self.calls, [])

    async def test_completion_forwards_options_and_releases_slot(self):
        result = await self.client.post("/v1/chat/completions", json={**self.payload, "max_completion_tokens": 20, "temperature": 0.2})
        self.assertEqual(result.status_code, 200)
        forwarded = json.loads(self.calls[0].content)
        self.assertEqual(forwarded["model"], self.settings.model)
        self.assertEqual(forwarded["max_tokens"], 20)
        self.assertEqual(forwarded["temperature"], 0.2)
        self.assertNotIn("max_completion_tokens", forwarded)
        self.assertEqual(self.app.state.manager.active, 0)

    async def test_stream_preserves_sse_and_split_done(self):
        chunks = (b'data: {"choices":[{"delta":{"content":"Hola"}}]}\n\n', b'data: [DO', b'NE]\n\n')
        self.handler = lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(*chunks))
        response = await self.client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertEqual(response.content, b"".join(chunks))
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(self.app.state.manager.active, 0)

    async def test_truncated_stream_reports_error(self):
        self.handler = lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(b'data: {}\n\n'))
        response = await self.client.post("/v1/chat/completions", json={**self.payload, "stream": True})
        self.assertIn(b'"error"', response.content)
        self.assertEqual(self.app.state.manager.active, 0)

    async def test_validation_happens_before_backend(self):
        cases = [
            ({**self.payload, "model": "arbitrary/download"}, 400),
            ({"messages": []}, 422),
            ({**self.payload, "max_tokens": 999999}, 422),
            ({**self.payload, "max_tokens": 2, "max_completion_tokens": 3}, 422),
            ({"messages": [{"role": "user", "content": "x" * 12001}]}, 413),
            ({**self.payload, "unexpected": True}, 422),
        ]
        for payload, code in cases:
            with self.subTest(payload=list(payload)):
                response = await self.client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, code)
        self.assertEqual(self.calls, [])

    async def test_large_chunked_body_is_rejected(self):
        async def body():
            yield b"x" * 600_000
            yield b"x" * 600_000
        response = await self.client.post("/v1/chat/completions", content=body())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])

    async def test_upstream_errors_are_not_all_changed_to_500(self):
        for code in (400, 429, 503):
            self.handler = lambda req, code=code: httpx.Response(code, json={"error": "test"})
            response = await self.client.post("/v1/chat/completions", json=self.payload)
            self.assertEqual(response.status_code, code)
            self.assertEqual(self.app.state.manager.active, 0)

    async def test_timeout_and_invalid_json_are_handled(self):
        def timeout(req):
            raise httpx.ReadTimeout("timeout", request=req)
        self.handler = timeout
        response = await self.client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 504)
        self.handler = lambda req: httpx.Response(200, text="invalid")
        response = await self.client.post("/v1/chat/completions", json=self.payload)
        self.assertEqual(response.status_code, 502)

    async def test_waiting_request_timeout_does_not_abort_active_request(self):
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def slow(req):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json={"choices": []})

        self.handler = slow
        with patch.object(self.app.state.manager, "abort", new_callable=AsyncMock) as abort:
            first = asyncio.create_task(self.client.post("/v1/chat/completions", json=self.payload))
            await entered.wait()
            second = await self.client.post("/v1/chat/completions", json=self.payload)
            self.assertEqual(second.status_code, 429)
            self.assertEqual(self.app.state.manager.active, 1)
            abort.assert_not_awaited()
            finish.set()
            self.assertEqual((await first).status_code, 200)
            self.assertEqual(self.app.state.manager.active, 0)

    async def test_external_mode_never_claims_it_unloaded_model(self):
        response = await self.client.post("/v1/unload", json={})
        self.assertEqual(response.status_code, 409)
