"""Terminal client and local server launcher. Never imports MLX in the client."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import ROOT, Settings


def request(base, path, payload=None, timeout=5):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=body, headers={"Content-Type": "application/json"})
    # Loopback requests must not accidentally go through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def read_json(base, path, payload=None):
    with request(base, path, payload) as response:
        return json.load(response)


def ask(base, messages, max_tokens=None):
    payload = {"model": "default_model", "messages": messages, "stream": True}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    pieces = []
    finished = False
    with request(base, "/v1/chat/completions", payload, timeout=300) as response:
        for line in response:
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                finished = True
                break
            event = json.loads(data)
            if "error" in event:
                raise RuntimeError(event["error"].get("message", str(event["error"])))
            for choice in event.get("choices", []):
                text = choice.get("delta", {}).get("content") or ""
                pieces.append(text)
                print(text, end="", flush=True)
    print()
    if not finished:
        raise RuntimeError("La respuesta se interrumpió antes de terminar")
    return "".join(pieces)


def start_server(settings, base):
    try:
        status = read_json(base, "/health")
    except (urllib.error.URLError, TimeoutError, ValueError):
        status = None
    if status is not None:
        if status.get("service") != "local-ia":
            raise RuntimeError(f"El puerto {settings.port} pertenece a otro servicio")
        print(f"Local-IA ya está disponible en {base}")
        return
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.runtime_dir / "api.log"
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with log_path.open("ab") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "core.cli", "serve"], cwd=ROOT,
            stdout=output, stderr=subprocess.STDOUT, env=env,
            start_new_session=os.name != "nt",
        )
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError(f"La API no pudo iniciar. Revisa {log_path}")
        try:
            status = read_json(base, "/health")
            if status.get("service") == "local-ia" and status.get("pid") == child.pid:
                (settings.runtime_dir / "api.pid").write_text(str(child.pid))
                print(f"Local-IA disponible en {base}; el modelo se cargará con la primera consulta.")
                return
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        time.sleep(0.1)
    child.terminate()
    child.wait(timeout=10)
    raise RuntimeError(f"No se pudo verificar el inicio. Revisa {log_path}")


def stop_server(settings, base):
    pid_path = settings.runtime_dir / "api.pid"
    if not pid_path.exists():
        raise RuntimeError("No hay una API iniciada con 'start'. Si usaste 'serve', detenla con Ctrl+C.")
    pid = int(pid_path.read_text().strip())
    try:
        status = read_json(base, "/health")
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("No se pudo verificar la API. No se detuvo ningún proceso.")
    if status.get("service") != "local-ia" or status.get("pid") != pid:
        raise RuntimeError("El PID guardado no coincide con la API. No se detuvo ningún proceso.")
    if status["backend"]["active_requests"] or status["backend"]["queued_requests"]:
        raise RuntimeError("Hay consultas en curso. Espera antes de detener la API.")
    # Unload through the API before stopping, so the model is never left behind.
    if settings.mode == "managed":
        read_json(base, "/v1/unload", {})
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
            print("API detenida.")
            return
        time.sleep(0.1)
    raise RuntimeError("Se solicitó el cierre; revisa .local-ia/api.log si tarda en terminar.")


def main():
    parser = argparse.ArgumentParser(description="Local-IA: IA desde tu terminal, con memoria bajo demanda")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Ejecutar la API en primer plano")
    commands.add_parser("start", help="Iniciar la API en segundo plano")
    commands.add_parser("stop", help="Detener la API iniciada con start")
    commands.add_parser("status", help="Ver el estado sin cargar el modelo")
    commands.add_parser("unload", help="Liberar ahora la memoria del motor")
    chat = commands.add_parser("chat", help="Conversar con respuestas en tiempo real")
    chat.add_argument("prompt", nargs="?", help="Una consulta; omítela para abrir un chat")
    chat.add_argument("--max-tokens", type=int)
    args = parser.parse_args()
    settings = Settings.from_env()
    base = f"http://127.0.0.1:{settings.port}"
    try:
        if args.command == "serve":
            import uvicorn
            uvicorn.run("core.main:app", host="127.0.0.1", port=settings.port, workers=1,
                        timeout_graceful_shutdown=10)
        elif args.command == "start":
            start_server(settings, base)
        elif args.command == "stop":
            stop_server(settings, base)
        elif args.command == "status":
            print(json.dumps(read_json(base, "/v1/status"), indent=2, ensure_ascii=False))
        elif args.command == "unload":
            print(json.dumps(read_json(base, "/v1/unload", {}), indent=2, ensure_ascii=False))
        elif args.command == "chat":
            messages = [{"role": "system", "content": "Eres un asistente útil. Responde en español, de forma clara y breve."}]
            if args.prompt:
                messages.append({"role": "user", "content": args.prompt})
                ask(base, messages, args.max_tokens)
                return
            print("Local-IA · /salir para terminar · /nuevo para limpiar el historial")
            while True:
                try:
                    prompt = input("\nTú: ").strip()
                except EOFError:
                    break
                if prompt == "/salir":
                    break
                if prompt == "/nuevo":
                    messages = messages[:1]
                    print("Conversación nueva.")
                    continue
                if not prompt:
                    continue
                candidate = messages + [{"role": "user", "content": prompt}]
                try:
                    print("IA: ", end="", flush=True)
                    answer = ask(base, candidate, args.max_tokens)
                    messages = candidate + [{"role": "assistant", "content": answer}]
                except urllib.error.HTTPError as exc:
                    print(f"\nError {exc.code}: {exc.read().decode()}", file=sys.stderr)
                    print("Usa /nuevo si el historial excede el límite.", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        print(f"Error {exc.code}: {exc.read().decode()}", file=sys.stderr)
        raise SystemExit(1)
    except (urllib.error.URLError, TimeoutError):
        print("No se pudo conectar con Local-IA. Ejecuta ./local-ia start.", file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nConsulta cancelada.")


if __name__ == "__main__":
    main()
