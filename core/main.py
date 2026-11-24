"""API compatible with text chat completions; MLX stays in a disposable worker."""

import os
import platform
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backend import BackendManager
from .config import Settings


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=120_000)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_usage: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "default_model"
    messages: list[Message] = Field(min_length=1, max_length=128)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1)
    stop: str | list[str] | None = None
    stream_options: StreamOptions | None = None

    @model_validator(mode="after")
    def check_options(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Usa max_tokens o max_completion_tokens, no ambos")
        if self.stop is not None:
            stops = [self.stop] if isinstance(self.stop, str) else self.stop
            if len(stops) > 8 or any(not item or len(item) > 200 for item in stops):
                raise ValueError("stop admite hasta 8 textos de 1 a 200 caracteres")
        return self


class OwnedStreamingResponse(StreamingResponse):
    """Keep the admission slot and HTTP connection until sending really finishes."""

    def __init__(self, content, cleanup):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.cleanup()


class BodyLimit:
    """Bound JSON parsing too, including requests without Content-Length."""

    def __init__(self, app, limit=1_000_000):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await JSONResponse({"detail": "La solicitud supera 1 MB"}, 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        timeout = httpx.Timeout(settings.request_timeout, connect=5, pool=5)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, transport=transport) as client:
            manager = BackendManager(settings, client)
            app.state.manager = manager
            app.state.client = client
            await manager.start()
            try:
                yield
            finally:
                await manager.close()

    app = FastAPI(title="Local-IA", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)

    @app.get("/health")
    @app.get("/v1/status")
    async def status(request: Request):
        return {
            "status": "ok", "service": "local-ia", "version": "0.2.0",
            "pid": os.getpid(), "platform": platform.system(),
            "backend": request.app.state.manager.snapshot(),
            "limits": {"max_tokens": settings.max_tokens, "max_input_chars": settings.max_input_chars,
                       "max_queue": settings.max_queue, "concurrency": 1},
        }

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": settings.model, "object": "model", "owned_by": "local"}]}

    @app.post("/v1/unload")
    async def unload(request: Request):
        return {"backend": await request.app.state.manager.unload()}

    async def check_upstream(response):
        if response.is_error:
            body = (await response.aread()).decode("utf-8", errors="replace")
            raise HTTPException(response.status_code, f"El motor respondió: {body[:1000]}")

    @app.post("/v1/chat/completions")
    async def chat(req: ChatRequest, request: Request):
        if req.model not in {"default", "default_model", settings.model}:
            raise HTTPException(400, "Modelo no configurado. Consulta GET /v1/models.")
        tokens = req.max_completion_tokens or req.max_tokens or settings.default_tokens
        if tokens > settings.max_tokens:
            raise HTTPException(422, f"El máximo permitido es {settings.max_tokens} tokens de salida.")
        if sum(len(m.content) for m in req.messages) > settings.max_input_chars:
            raise HTTPException(413, f"El historial supera {settings.max_input_chars} caracteres. Inicia otra conversación o reduce el texto.")

        payload = req.model_dump(exclude_none=True, exclude={"max_completion_tokens"})
        payload.update(model=settings.model, max_tokens=tokens)
        manager = request.app.state.manager
        client = request.app.state.client
        stack = AsyncExitStack()
        handed_off = False
        admitted = False
        completed = False

        async def cleanup():
            with anyio.CancelScope(shield=True):
                # MLX may still be computing even if its HTTP client disconnects.
                # Kill only our own worker before allowing the next queued request.
                if admitted and not completed:
                    await manager.abort()
                await stack.aclose()

        try:
            await stack.enter_async_context(manager.request_slot())
            admitted = True
            upstream_request = client.build_request("POST", f"{settings.backend_url}/v1/chat/completions", json=payload)
            response = await client.send(upstream_request, stream=req.stream)
            stack.push_async_callback(response.aclose)
            await check_upstream(response)

            if not req.stream:
                try:
                    result = response.json()
                except ValueError as exc:
                    raise HTTPException(502, "El motor devolvió una respuesta que no es JSON") from exc
                completed = True
                return JSONResponse(result)

            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise HTTPException(502, "El motor no devolvió un flujo SSE válido")

            async def events():
                nonlocal completed
                tail = b""
                try:
                    async for chunk in response.aiter_bytes():
                        tail += chunk
                        if b"data: [DONE]" in tail:
                            completed = True
                        tail = tail[-64:]
                        yield chunk
                    if not completed:
                        yield 'data: {"error":{"message":"El motor interrumpió la respuesta"}}\n\n'.encode()
                except httpx.HTTPError:
                    yield 'data: {"error":{"message":"Se perdió la conexión con el motor"}}\n\n'.encode()

            result = OwnedStreamingResponse(events(), cleanup)
            handed_off = True
            return result
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "El motor tardó demasiado en responder. Revisa .local-ia/backend.log.") from exc
        except httpx.RequestError as exc:
            raise HTTPException(503, "No se pudo conectar con el motor. Consulta /v1/status y .local-ia/backend.log.") from exc
        finally:
            if not handed_off:
                await cleanup()

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("core.main:app", host="127.0.0.1", port=Settings.from_env().port, workers=1)
