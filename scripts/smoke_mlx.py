"""Opt-in integration check with an already cached model on Apple silicon.

Run with the API's Python and LOCAL_IA_MLX_PYTHON pointing to an MLX interpreter.
Uses separate ports and always stops its own API/worker at the end.
"""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:18001"


def call(path, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for condition")


def rss(pid):
    result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return round(int(result.stdout.strip()) / 1024, 1) if result.stdout.strip() else 0


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main():
    report = {}
    with tempfile.TemporaryDirectory(prefix="local-ia-smoke-") as runtime:
        env = dict(os.environ)
        env.update(LOCAL_IA_PORT="18001", LOCAL_IA_BACKEND_PORT="18080", LOCAL_IA_IDLE_SECONDS="3",
                   LOCAL_IA_MODE="managed", LOCAL_IA_OFFLINE="1", LOCAL_IA_RUNTIME_DIR=runtime)
        with open(Path(runtime) / "api.log", "wb") as output:
            process = subprocess.Popen([sys.executable, "-m", "core.cli", "serve"], cwd=ROOT,
                                       env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            initial = wait_for(lambda: call("/health"))
            assert initial["backend"]["state"] == "stopped", initial
            assert initial["backend"]["pid"] is None, initial
            report["api_idle_rss_mb"] = rss(initial["pid"])
            print("PASS: API starts without MLX", flush=True)

            prompt = {"messages": [{"role": "user", "content": "Responde en español: ¿cuánto es dos más dos?"}], "max_tokens": 32}
            start = time.monotonic()
            result = call("/v1/chat/completions", prompt)
            report["cold_request_seconds"] = round(time.monotonic() - start, 2)
            report["answer"] = result["choices"][0]["message"]["content"]
            loaded = call("/v1/status")
            first_pid = loaded["backend"]["pid"]
            assert first_pid and loaded["backend"]["starts"] == 1, loaded
            report["worker_rss_mb"] = rss(first_pid)
            print("PASS: cached model loads and answers: " + report["answer"], flush=True)

            req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps({**prompt, "stream": True}).encode(),
                                         headers={"Content-Type": "application/json"})
            events = []
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=120) as response:
                for line in response:
                    if line.startswith(b"data:"):
                        events.append(line[5:].strip())
                        if len(events) == 1:
                            report["warm_first_event_seconds"] = round(time.monotonic() - start, 3)
            assert events[-1] == b"[DONE]", events
            assert len(events) > 2, events
            report["stream_events"] = len(events)
            assert call("/v1/status")["backend"]["pid"] == first_pid
            print("PASS: incremental streaming reuses the worker", flush=True)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: call("/v1/chat/completions", prompt), range(2)))
            assert all(result["choices"] for result in results)
            assert call("/v1/status")["backend"]["starts"] == 1
            print("PASS: concurrent requests share one worker", flush=True)

            long_prompt = {"messages": [{"role": "user", "content": "Cuenta del 1 al 1000 sin parar, escribe todos los números separados por comas."}],
                           "stream": True, "max_tokens": 1024}
            req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(long_prompt).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as response:
                for line in response:
                    if line.startswith(b"data:"):
                        break
            wait_for(lambda: call("/v1/status")["backend"]["active_requests"] == 0)
            wait_for(lambda: not alive(first_pid), timeout=10)
            print("PASS: disconnect cancels and releases the owned worker", flush=True)

            result = call("/v1/chat/completions", prompt)
            loaded = call("/v1/status")
            second_pid = loaded["backend"]["pid"]
            assert second_pid != first_pid
            call("/v1/unload", {})
            assert not alive(second_pid)
            print("PASS: restart and manual unload", flush=True)

            call("/v1/chat/completions", prompt)
            third_pid = call("/v1/status")["backend"]["pid"]
            wait_for(lambda: call("/v1/status")["backend"]["state"] == "stopped", timeout=10)
            assert not alive(third_pid)
            final = call("/v1/status")
            report["api_after_unload_rss_mb"] = rss(final["pid"])
            report["final_backend"] = final["backend"]
            print("PASS: idle timeout terminates model process; API remains available", flush=True)

            report_path = ROOT / ".local-ia" / "smoke-report.json"
            report_path.parent.mkdir(exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        except BaseException:
            for path in (Path(runtime) / "api.log", Path(runtime) / "backend.log"):
                if path.exists():
                    print(f"\n{path.name}:\n{path.read_text(errors='replace')[-6000:]}", file=sys.stderr)
            raise
        finally:
            process.terminate()
            process.wait(timeout=20)


if __name__ == "__main__":
    main()
