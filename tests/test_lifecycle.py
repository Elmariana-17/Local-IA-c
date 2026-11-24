import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from core.backend import BackendManager
from core.config import Settings


class FakeProcess:
    pid = 123456

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminate()

    async def wait(self):
        return self.returncode


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = replace(Settings(), runtime_dir=Path(self.temp.name), idle_seconds=0.04, queue_timeout=1)
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
        self.manager = BackendManager(self.settings, self.client)
        self.manager._port_in_use = AsyncMock(return_value=False)
        self.spawn = patch("core.backend.asyncio.create_subprocess_exec", new_callable=AsyncMock)
        self.mock_spawn = self.spawn.start()
        self.mock_spawn.side_effect = lambda *args, **kwargs: FakeProcess()
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        await self.client.aclose()
        self.spawn.stop()
        self.temp.cleanup()

    async def test_lazy_start_reuse_idle_stop_and_restart(self):
        self.mock_spawn.assert_not_awaited()
        async with self.manager.request_slot():
            first = self.manager.process
        async with self.manager.request_slot():
            self.assertIs(self.manager.process, first)
        self.assertEqual(self.mock_spawn.await_count, 1)
        await asyncio.sleep(0.12)
        self.assertTrue(first.terminated)
        self.assertIsNone(self.manager.process)
        async with self.manager.request_slot():
            self.assertIsNot(self.manager.process, first)
        self.assertEqual(self.mock_spawn.await_count, 2)

    async def test_idle_timer_and_manual_unload_cannot_interrupt_request(self):
        async with self.manager.request_slot():
            process = self.manager.process
            await asyncio.sleep(0.12)
            self.assertFalse(process.terminated)
            with self.assertRaises(HTTPException) as raised:
                await self.manager.unload()
            self.assertEqual(raised.exception.status_code, 409)
        await self.manager.unload()
        self.assertTrue(process.terminated)

    async def test_simultaneous_requests_share_one_child_and_serialize(self):
        active = 0
        peak = 0

        async def work():
            nonlocal active, peak
            async with self.manager.request_slot():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.015)
                active -= 1

        await asyncio.gather(*(work() for _ in range(4)))
        self.assertEqual(peak, 1)
        self.assertEqual(self.mock_spawn.await_count, 1)
        self.assertEqual(self.manager.waiting, 0)

    async def test_cancellation_frees_model_and_slot(self):
        entered = asyncio.Event()

        async def work():
            async with self.manager.request_slot():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        process = self.manager.process
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(process.terminated)
        self.assertEqual(self.manager.active, 0)
        async with self.manager.request_slot():
            self.assertIsNot(self.manager.process, process)

    async def test_start_failure_releases_slot_for_retry(self):
        self.mock_spawn.side_effect = FileNotFoundError("python")
        with self.assertRaises(HTTPException) as raised:
            async with self.manager.request_slot():
                pass
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.manager.active, 0)
        self.mock_spawn.side_effect = lambda *args, **kwargs: FakeProcess()
        async with self.manager.request_slot():
            self.assertIsNotNone(self.manager.process)

    async def test_occupied_port_is_not_adopted_or_terminated(self):
        self.manager._port_in_use.return_value = True
        with self.assertRaises(HTTPException):
            async with self.manager.request_slot():
                pass
        self.mock_spawn.assert_not_awaited()

    async def test_second_manager_cannot_own_same_backend(self):
        other = BackendManager(self.settings, self.client)
        with self.assertRaises(RuntimeError):
            await other.start()
        await other.close()

    async def test_shutdown_stops_owned_worker(self):
        async with self.manager.request_slot():
            process = self.manager.process
        await self.manager.close()
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
