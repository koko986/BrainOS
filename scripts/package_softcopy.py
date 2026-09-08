"""Build a source-only handover without private runtime data or model weights."""

from pathlib import Path
import re
import zipfile


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliverables" / "MARLIN_Project_Softcopy.zip"
SOURCE_DIRS = ("marlin", "second_brain", "prolog", "tests", "docs", "scripts", "presentation")
ROOT_FILES = ("main.py", "README.md", "requirements.txt", ".env.example", ".gitignore", "run_marlin.bat", "run_second_brain_demo.bat")
SKIP_PARTS = {"__pycache__", ".pytest_cache", "node_modules", ".git", ".venv", "venv"}
EXTENSIONS = {".py", ".pl", ".md", ".txt", ".js", ".css", ".html", ".svg", ".json", ".bat"}
SECRET = re.compile(rb"(?:gsk_[A-Za-z0-9]{24,}|sk-(?:or-v1-)?[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{30,})")


def main():
    paths = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    for directory in SOURCE_DIRS:
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and not path.is_symlink()
                     and not SKIP_PARTS.intersection(path.relative_to(ROOT).parts)
                     and path.suffix.lower() in EXTENSIONS)
    paths = sorted(set(paths))
    for path in paths:
        if SECRET.search(path.read_bytes()):
            raise RuntimeError(f"Potential credential detected; review before sharing: {path.relative_to(ROOT)}")
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, "MARLIN/" + path.relative_to(ROOT).as_posix())
        archive.writestr("MARLIN/data/database/.gitkeep", "")
        archive.writestr("MARLIN/SOURCE_PACKAGE.txt", "Current working source, including uncommitted updates.\n"
                         "Excluded: private .env, databases, models, browser profiles, logs, caches and Git history.\n"
                         "Start with presentation/MARLIN_Project_Guide.md.\n"
                         "Models and external runtimes must be installed separately.\n")
    with zipfile.ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert "MARLIN/main.py" in names and "MARLIN/prolog/rules.pl" in names
        assert not any(name.endswith(("/.env", ".db", ".log", ".pyc")) for name in names)
    print(f"Created {OUTPUT}")
    print(f"Verified {len(names)} archive entries; {OUTPUT.stat().st_size:,} bytes.")


if __name__ == "__main__":
    main()
