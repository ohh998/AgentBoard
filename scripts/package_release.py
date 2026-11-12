"""Build a clean, reproducible portable ZIP from the committed Git tree."""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {".gitattributes", ".gitignore", "AGENTS.md", "agent.md", "README.md", "CHANGELOG.md",
              "CONTRACT.md", "PORTABLE.md", "start.cmd", "start.sh", "agentctl.py"}
SOURCE_DIRS = {"agentboard", "web", "docs", "examples", "scripts", "tests"}
FORBIDDEN_PARTS = {".git", "credentials", "__pycache__", ".venv", "venv", "node_modules", "releases"}
FORBIDDEN_SUFFIXES = {".token", ".sqlite", ".sqlite3", ".db", ".pyc", ".pyo", ".log", ".zip", ".pem", ".key", ".pfx", ".lock"}


def git(*args):
    result = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}", "-C", str(ROOT), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError("Git command failed; run this builder inside a committed project checkout")
    return result.stdout


def safe_source(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise RuntimeError(f"Unsafe archive path: {name}")
    if any(p.casefold() in FORBIDDEN_PARTS or p.casefold().startswith(".agentboard") or p.casefold().startswith(".env") for p in path.parts):
        raise RuntimeError(f"Runtime or private path tracked in Git: {name}")
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES or path.name.casefold().endswith(("-wal", "-shm", "-journal")):
        raise RuntimeError(f"Unexpected runtime or private file tracked in Git: {name}")
    if name not in ROOT_FILES and (len(path.parts) < 2 or path.parts[0] not in SOURCE_DIRS):
        raise RuntimeError(f"Review and update the release allowlist for: {name}")


def build(output_dir, force=False):
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise RuntimeError("Commit tracked source changes before building a versioned release")
    commit = git("rev-parse", "HEAD").decode().strip()
    timestamp = int(git("show", "-s", "--format=%ct", "HEAD").decode().strip())
    sources = {}
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD")), mode="r:") as archive:
        for item in archive.getmembers():
            if item.isdir():
                continue
            if not item.isfile():
                raise RuntimeError("Portable releases do not allow links or special files")
            safe_source(item.name)
            content = archive.extractfile(item).read()
            if item.name.endswith(".cmd"):
                content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            sources[item.name] = content
            modes[item.name] = 0o755 if item.name == "start.sh" else 0o644
    version = None
    for node in ast.parse(sources["agentboard/__init__.py"].decode("utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            version = ast.literal_eval(node.value)
    if not isinstance(version, str) or not version or any(c not in "0123456789.-abcdefghijklmnopqrstuvwxyz" for c in version):
        raise RuntimeError("Invalid application version")
    folder = f"agentboard-{version}"
    manifest = {"format": 1, "application": "Agent Board", "version": version, "source_commit": commit,
                "source_committed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "python_required": "3.11+ with sqlite3", "bundled_python": False,
                "files": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                          for name, content in sorted(sources.items())]}
    sources["RELEASE-MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{folder}-portable.zip"
    checksum = target.with_suffix(".zip.sha256")
    if not force and (target.exists() or checksum.exists()):
        raise RuntimeError("Release already exists; use --force to replace the same version")
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", suffix=".tmp", dir=output_dir)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in sorted(sources.items()):
                info = zipfile.ZipInfo(folder + "/" + name, datetime.fromtimestamp(max(timestamp, 315532800), timezone.utc).timetuple()[:6])
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | modes.get(name, 0o644)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content, compresslevel=9)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP integrity validation failed")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
    checksum.write_text(f"{sha256}  {target.name}\n", encoding="ascii")
    return {"archive": str(target), "sha256_file": str(checksum), "sha256": sha256,
            "bytes": target.stat().st_size, "files": len(sources), "source_commit": commit, "version": version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "releases")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output_dir, args.force), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Release failed: {exc}\n")


if __name__ == "__main__":
    main()
