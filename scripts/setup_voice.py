"""Download the small offline Vosk model used for MARLIN's wake word.

Run once before using hands-free mode:

    python scripts/setup_voice.py
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = PROJECT_ROOT / "models"
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"


def download(url: str, destination: Path) -> None:
    print(f"Downloading {url}")
    try:
        _download_with_urllib(url, destination)
        return
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        print(f"  Python TLS download failed ({exc}). Retrying with curl.")

    # Local TLS interception (antivirus or a corporate proxy) makes Python
    # reject the certificate chain even with certifi. curl uses the Windows
    # certificate store, which does trust the interception root.
    _download_with_curl(url, destination)


def _download_with_urllib(url: str, destination: Path) -> None:
    context = ssl.create_default_context(cafile=certifi.where())
    request = Request(url, headers={"User-Agent": "MARLIN-BrainOS/0.1"})
    with urlopen(request, timeout=120, context=context) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(262_144)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if total:
                    percent = downloaded * 100 // total
                    print(f"  {percent:3d}%  {downloaded // 1024} KB", end="\r", flush=True)
    print()


def _download_with_curl(url: str, destination: Path) -> None:
    try:
        result = subprocess.run(
            [
                "curl",
                "--location",
                "--fail",
                "--progress-bar",
                "--max-time",
                "600",
                "--output",
                str(destination),
                url,
            ],
            check=False,
        )
    except OSError as exc:
        raise OSError(f"curl is not available: {exc}") from exc
    if result.returncode != 0:
        raise OSError(f"curl exited with code {result.returncode}")
    if not destination.exists() or destination.stat().st_size < 1_000_000:
        raise OSError("Downloaded archive looks incomplete.")


def main() -> int:
    target = MODELS_DIR / MODEL_NAME
    if (target / "am").exists() or (target / "conf").exists():
        print(f"Model already installed: {target}")
        return 0

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work_dir:
        archive = Path(work_dir) / f"{MODEL_NAME}.zip"
        try:
            download(MODEL_URL, archive)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            print(f"Download {MODEL_URL} manually and unzip it into {MODELS_DIR}.", file=sys.stderr)
            return 2

        print("Extracting...")
        extract_dir = Path(work_dir) / "extracted"
        try:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"Could not extract the model: {exc}", file=sys.stderr)
            return 2

        extracted = extract_dir / MODEL_NAME
        if not extracted.exists():
            folders = [item for item in extract_dir.iterdir() if item.is_dir()]
            if not folders:
                print("Archive did not contain a model folder.", file=sys.stderr)
                return 2
            extracted = folders[0]

        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))

    print(f"Voice model ready: {target}")
    print("Set SECOND_BRAIN_VOSK_MODEL_PATH=models/" + MODEL_NAME + " in .env if it is not already set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
