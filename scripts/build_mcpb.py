"""
Build the Claude Desktop extension: dist/arv-mcp.mcpb.

An .mcpb file is a zip with manifest.json at its root. arv-mcp uses the MCPB
"uv" server type: the bundle carries the package source, pyproject.toml and
a uv.lock, and Claude Desktop installs Python and the locked dependencies
itself, so users need nothing installed beforehand.

    python scripts/build_mcpb.py            # needs `uv` on PATH (for uv.lock)
    python scripts/build_mcpb.py --no-lock  # skip the lock (tests only)

The manifest's version is stamped from src/arv_mcp/__init__.py, so the
version still lives in one place. The zip uses sorted entries and fixed
timestamps, so the same inputs always produce the same file.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging" / "mcpb"
BUNDLE_NAME = "arv-mcp.mcpb"
TOP_LEVEL_FILES = ("pyproject.toml", "README.md", "LICENSE")
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def package_version() -> str:
    text = (ROOT / "src" / "arv_mcp" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def stamped_manifest() -> dict:
    manifest = json.loads((PACKAGING / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = package_version()
    return manifest


def stage(dest: Path, *, lock: bool = True) -> None:
    """Lay out the bundle's contents in `dest`."""
    for name in TOP_LEVEL_FILES:
        shutil.copy2(ROOT / name, dest / name)
    shutil.copytree(ROOT / "src", dest / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    shutil.copy2(PACKAGING / "icon.png", dest / "icon.png")
    (dest / "manifest.json").write_text(json.dumps(stamped_manifest(), indent=2) + "\n", encoding="utf-8")
    if lock:
        # Universal lock (all platforms), resolved now so installs match what was tested.
        subprocess.run(["uv", "lock", "--quiet"], cwd=dest, check=True)


def write_zip(src_dir: Path, out: Path) -> None:
    files = sorted(p for p in src_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(src_dir).as_posix(), date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def build(out_dir: Path, *, lock: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / BUNDLE_NAME
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        stage(staging, lock=lock)
        write_zip(staging, out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output directory (default: dist/)")
    parser.add_argument("--no-lock", action="store_true", help="skip generating uv.lock (tests only)")
    args = parser.parse_args()
    out = build(args.out, lock=not args.no_lock)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"{out}  {out.stat().st_size:,} bytes  sha256={digest}")


if __name__ == "__main__":
    main()
