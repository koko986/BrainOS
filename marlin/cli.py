"""Single launcher for MARLIN V2 desktop, terminal, voice, and compatibility commands."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from marlin.config import MarlinSettings
from marlin.runtime import MarlinRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marlin", description="MARLIN V2 fully local JARVIS assistant")
    parser.add_argument("--db", dest="database_path", help="Override the SQLite brain database path.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("desktop", help="Open the native desktop cockpit.")
    serve = sub.add_parser("serve", help="Run the browser cockpit.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    sub.add_parser("terminal", aliases=["marlin"], help="Run terminal conversation mode.")
    sub.add_parser("voice", aliases=["jarvis"], help="Run hands-free wake-word mode.")
    setup = sub.add_parser("setup", help="Install local dependencies and download models.")
    setup.add_argument("--launch-on-login", action="store_true", help="Start MARLIN voice mode at Windows login.")
    sub.add_parser("doctor", help="Check every local MARLIN subsystem.")
    ask = sub.add_parser("ask", help="Run one MARLIN command.")
    ask.add_argument("text")
    sub.add_parser("seed-demo")
    sub.add_parser("list-entities")
    sub.add_parser("list-relationships")
    reason = sub.add_parser("reason")
    reason_sub = reason.add_subparsers(dest="reason_command", required=True)
    reason_sub.add_parser("important-tasks")
    reason_sub.add_parser("high-priority")
    why = reason_sub.add_parser("why-high-priority")
    why.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = MarlinSettings.from_env()
    if args.database_path:
        settings.database_path = Path(args.database_path).expanduser()
    command = args.command or "desktop"

    if command == "setup":
        if args.launch_on_login:
            settings.launch_on_login = True
        return run_setup(settings)
    if command == "doctor":
        return run_doctor(settings)

    if command not in {"desktop", "serve", "voice", "jarvis"}:
        settings.wake_word_enabled = False
    runtime = MarlinRuntime(settings)
    if command == "desktop":
        run_desktop(runtime)
        return 0
    if command == "serve":
        from marlin.web import run_server
        try:
            run_server(runtime, args.host or settings.host, args.port or settings.port)
        finally:
            runtime.shutdown()
        return 0
    if command in {"terminal", "marlin"}:
        run_terminal(runtime)
        return 0
    if command in {"voice", "jarvis"}:
        run_voice(runtime)
        return 0
    if command == "ask":
        print(runtime.command(args.text, source="cli")["message"])
        return 0
    if command == "seed-demo":
        result = runtime.knowledge.seed_demo()
        print(f"Seeded {result.entities_created} entities and {result.relationships_created} relationships.")
        return 0
    if command == "list-entities":
        for entity in runtime.knowledge.list_entities():
            print(f"{entity.id}\t{entity.type}\t{entity.name}\t{entity.source}")
        return 0
    if command == "list-relationships":
        for relation in runtime.knowledge.list_relationships():
            print(f"{relation.id}\t{relation.source_id}\t{relation.type}\t{relation.target_id}")
        return 0
    if command == "reason":
        if args.reason_command == "important-tasks":
            tasks = runtime.reasoning.important_tasks()
            for task in tasks:
                print(f"{task.id}\t{task.name}")
        elif args.reason_command == "high-priority":
            tasks = runtime.reasoning.high_priority_tasks()
            for task in tasks:
                print(f"{task.id}\t{task.name}")
        else:
            explanation = runtime.reasoning.why_high_priority(args.task_id)
            print(explanation.title)
            for index, step in enumerate(explanation.steps, start=1):
                print(f"{index}. {step}")
        return 0
    return 1


def run_desktop(runtime: MarlinRuntime) -> None:
    from marlin.web import start_server_thread
    host = runtime.settings.host
    port = _available_port(host, runtime.settings.port)
    start_server_thread(runtime, host, port)
    url = f"http://{host}:{port}"
    _wait_for_server(url)
    print(f"MARLIN V2 cockpit: {url}")
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        print("pywebview is unavailable; opening the complete cockpit in your browser.")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            runtime.shutdown()
        return
    webview.create_window("MARLIN V2", url, width=1360, height=860, min_size=(820, 560))
    webview.start()
    runtime.shutdown()


def run_terminal(runtime: MarlinRuntime) -> None:
    print("MARLIN V2 is local and ready. Type `exit` to stop, `listen` for one voice command.")
    while True:
        try:
            text = input("MARLIN> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"exit", "quit", "stand down"}:
            break
        if text.lower() == "listen":
            try:
                heard = runtime.listen()
                print(f"You: {heard.get('text', '')}")
                if heard.get("result"):
                    print(f"MARLIN: {heard['result']['message']}")
            except Exception as exc:
                print(f"Voice unavailable: {exc}")
            continue
        result = runtime.command(text, source="terminal")
        print(f"MARLIN: {result['message']}")
        if result.get("pending"):
            answer = input(f"Approve {result['pending']['label']} on {result['pending']['target']}? [y/N] ").strip().lower()
            resolved = runtime.approve_action(result["pending"]["id"]) if answer in {"y", "yes"} else runtime.cancel_action(result["pending"]["id"])
            print(f"MARLIN: {resolved['message']}")
    runtime.shutdown()


def run_voice(runtime: MarlinRuntime) -> None:
    print("MARLIN V2 voice mode ready. Say Hey MARLIN, then speak after the acknowledgement. Ctrl+C stops.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        runtime.shutdown()


def run_setup(settings: MarlinSettings) -> int:
    setup_ok = True
    print("Installing MARLIN V2 local dependencies...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(Path(__file__).resolve().parents[1] / "requirements.txt")], check=False)
    if result.returncode:
        print("Dependency installation failed.", file=sys.stderr)
        return result.returncode
    if not shutil.which("ollama"):
        print("Ollama is not installed. Install it from https://ollama.com/download and run setup again.", file=sys.stderr)
        return 2
    print(f"Downloading local language model {settings.ollama_model}...")
    if subprocess.run(["ollama", "pull", settings.ollama_model], check=False).returncode:
        return 2
    print(f"Preparing Faster-Whisper {settings.whisper_model}...")
    try:
        import os
        import certifi
        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
        from faster_whisper import WhisperModel
        WhisperModel(settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type)
    except Exception as exc:
        print(f"Whisper setup failed: {exc}", file=sys.stderr)
        setup_ok = False
    settings.piper_data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Piper voice {settings.piper_voice}...")
    try:
        from piper.download_voices import download_voice
        download_voice(settings.piper_voice, settings.piper_data_dir)
    except Exception as exc:
        print(f"Piper voice download did not complete ({exc}); Windows local speech remains available as a fallback.")
        setup_ok = False
    if settings.launch_on_login:
        startup = _configure_launch_on_login()
        print(f"Windows login launch configured: {startup}")
    print("MARLIN V2 local setup is complete." if setup_ok else "MARLIN V2 setup is partial; see the failed checks above.")
    doctor_result = run_doctor(settings)
    return doctor_result if setup_ok else 2


def run_doctor(settings: MarlinSettings) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Python 3.11+", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("Ollama executable", bool(shutil.which("ollama")), shutil.which("ollama") or "missing"))
    checks.append(("SWI-Prolog", bool(shutil.which("swipl") or shutil.which("swipl.exe")), shutil.which("swipl") or shutil.which("swipl.exe") or "missing"))
    for module in ("fastapi", "uvicorn", "pyswip", "sounddevice", "vosk", "faster_whisper", "piper", "send2trash", "psutil"):
        checks.append((module, importlib.util.find_spec(module) is not None, "installed" if importlib.util.find_spec(module) else "missing"))
    checks.append(("Vosk wake model", settings.vosk_model_path.exists(), str(settings.vosk_model_path)))
    checks.append(("Piper voice model", (settings.piper_data_dir / f"{settings.piper_voice}.onnx").exists(), settings.piper_voice))
    checks.append(("Desktop wrapper", importlib.util.find_spec("webview") is not None, "pywebview"))
    checks.append(("NVIDIA GPU tools", bool(shutil.which("nvidia-smi")), shutil.which("nvidia-smi") or "not detected"))
    try:
        import sounddevice as sd
        microphone = sd.query_devices(kind="input")
        checks.append(("Microphone", bool(microphone), str(microphone.get("name", "available"))))
    except Exception as exc:
        checks.append(("Microphone", False, str(exc)))
    try:
        runtime = MarlinRuntime(settings, start_background=False)
        model = runtime.model.health()
        checks.append(("Ollama service", bool(model.get("available")), str(model.get("error") or settings.ollama_url)))
        checks.append(("Qwen local model", bool(model.get("loaded")), settings.ollama_model))
        with runtime.store.connect() as connection:
            fts = connection.execute("SELECT name FROM sqlite_master WHERE name='file_search_fts'").fetchone()
        checks.append(("SQLite brain + FTS", bool(fts), str(settings.database_path)))
        runtime.routine.stop()
    except Exception as exc:
        checks.append(("Runtime", False, str(exc)))
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'MISSING'}] {name}: {detail}")
    required = {"Python 3.11+", "Ollama executable", "fastapi", "uvicorn", "pyswip", "send2trash", "psutil", "SQLite brain + FTS"}
    return 0 if all(ok for name, ok, _ in checks if name in required) else 2


def _available_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    raise RuntimeError("No local MARLIN cockpit port is available.")


def _wait_for_server(url: str, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/api/state", timeout=.8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("version") == "2.0":
                return
        except (OSError, URLError, json.JSONDecodeError):
            time.sleep(.2)
    raise RuntimeError(f"MARLIN V2 did not start at {url}")


def _configure_launch_on_login() -> Path:
    import os
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    launcher = startup / "MARLIN-V2.cmd"
    root_main = Path(__file__).resolve().parents[1] / "main.py"
    launcher.write_text(f'@echo off\r\nstart "MARLIN V2" /min "{sys.executable}" "{root_main}" voice\r\n', encoding="utf-8")
    return launcher
