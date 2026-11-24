"""Own one MLX child process, serialize requests, and reclaim idle memory."""

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import HTTPException

from .config import Settings

log = logging.getLogger("local_ia")


class BackendManager:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.process = None
        self.active = 0
        self.waiting = 0
        self.starts = 0
        self.last_used = time.monotonic()
        self.last_error = None
        self._state = "stopped"
        self._gate = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._reaper = None
        self._lock_file = None

    async def start(self):
        """Reserve ownership, but keep all MLX imports/model allocation in the child."""
        if self.settings.mode == "managed":
            self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.runtime_dir / f"backend-{self.settings.backend_port}.lock"
            self._lock_file = path.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    self._lock_file.seek(0)
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._lock_file.close()
                self._lock_file = None
                raise RuntimeError("Ya hay un gestor de MLX. Inicia la API con un solo worker.")
            self._reaper = asyncio.create_task(self._reap(), name="local-ia-idle")

    def snapshot(self):
        running = self.process is not None and self.process.returncode is None
        state = self._state
        if self.settings.mode == "external":
            state = "external"
        elif self.process is not None and not running:
            state = "error"
        remaining = None
        if running and not self.active and not self.waiting:
            remaining = max(0, round(self.settings.idle_seconds - (time.monotonic() - self.last_used), 1))
        return {
            "mode": self.settings.mode,
            "state": state,
            "model": self.settings.model,
            "pid": self.process.pid if running else None,
            "active_requests": self.active,
            "queued_requests": self.waiting,
            "starts": self.starts,
            "idle_seconds": self.settings.idle_seconds,
            "unload_in_seconds": remaining,
            "last_error": self.last_error,
        }

    async def _port_in_use(self):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.settings.backend_port), 1
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        await writer.wait_closed()
        return True

    async def _ensure_running(self):
        if self.settings.mode == "external":
            return
        if self.process is not None and self.process.returncode is None:
            return
        if await self._port_in_use():
            raise HTTPException(503, f"Puerto {self.settings.backend_port} ocupado. Detén el motor anterior o configura LOCAL_IA_MODE=external.")

        self._state = "starting"
        self.last_error = None
        log_path = self.settings.runtime_dir / "backend.log"
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            log_path.replace(log_path.with_suffix(".log.1"))
        env = dict(os.environ)
        env["HF_HUB_OFFLINE"] = "1" if self.settings.offline else "0"
        env["TOKENIZERS_PARALLELISM"] = "false"
        command = [
            self.settings.mlx_python, "-m", "mlx_lm", "server",
            "--model", self.settings.model,
            "--host", "127.0.0.1", "--port", str(self.settings.backend_port),
            "--max-tokens", str(self.settings.default_tokens),
            "--decode-concurrency", "1", "--prompt-concurrency", "1",
            "--prompt-cache-size", "1", "--prompt-cache-bytes", "64MB",
            "--log-level", "WARNING",
        ]
        try:
            with log_path.open("ab") as output:
                self.process = await asyncio.create_subprocess_exec(
                    *command, stdout=output, stderr=asyncio.subprocess.STDOUT,
                    env=env, start_new_session=os.name != "nt",
                )
            self.starts += 1
            deadline = time.monotonic() + self.settings.startup_timeout
            while time.monotonic() < deadline:
                if self.process.returncode is not None:
                    raise RuntimeError(f"MLX terminó con código {self.process.returncode}")
                try:
                    response = await self.client.get(f"{self.settings.backend_url}/health", timeout=1)
                    if response.status_code == 200:
                        self._state = "running"
                        log.info("Motor MLX iniciado (pid %s)", self.process.pid)
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.15)
            raise RuntimeError("MLX no estuvo listo dentro del tiempo de inicio")
        except Exception as exc:
            self.last_error = f"{exc}. Revisa {log_path} y LOCAL_IA_MLX_PYTHON."
            await self._stop_locked()
            self._state = "error"
            raise HTTPException(503, self.last_error) from exc

    async def _stop_locked(self):
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            log.info("Motor MLX detenido; memoria del proceso liberada")
        self.process = None
        self._state = "stopped"

    async def abort(self):
        """Cancel computation too, including a disconnected client during prefill."""
        if self.settings.mode == "managed":
            with anyio.CancelScope(shield=True):
                async with self._lifecycle:
                    await self._stop_locked()

    @asynccontextmanager
    async def request_slot(self):
        if self.active + self.waiting >= self.settings.max_queue + 1:
            raise HTTPException(429, "Cola llena. Inténtalo de nuevo en unos segundos.", headers={"Retry-After": "5"})
        self.waiting += 1
        try:
            try:
                await asyncio.wait_for(self._gate.acquire(), self.settings.queue_timeout)
            except asyncio.TimeoutError as exc:
                raise HTTPException(429, "Se agotó el tiempo de espera en la cola.", headers={"Retry-After": "5"}) from exc
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            async with self._lifecycle:
                await self._ensure_running()
            yield
        except BaseException:
            await self.abort()
            raise
        finally:
            self.active -= 1
            self.last_used = time.monotonic()
            self._gate.release()

    async def unload(self):
        if self.settings.mode == "external":
            raise HTTPException(409, "El motor externo administra su propia memoria; esta API no lo detiene.")
        async with self._lifecycle:
            if self.active or self.waiting:
                raise HTTPException(409, "Hay consultas en curso. Espera a que terminen.")
            await self._stop_locked()
        return self.snapshot()

    async def _reap(self):
        while True:
            await asyncio.sleep(min(self.settings.idle_seconds, 2))
            async with self._lifecycle:
                if not self.active and not self.waiting and time.monotonic() - self.last_used >= self.settings.idle_seconds:
                    await self._stop_locked()

    async def close(self):
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        await self.abort()
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None
