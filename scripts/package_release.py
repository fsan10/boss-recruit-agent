"""Package tracked source, hashes, and local git history; never runtime or credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT.parent / "BOSS_RECRUIT_AGENT_V0_1.zip"))
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    files = [name for name in files if name]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise SystemExit("Commit all source changes before packaging.")
    if any(Path(name).parts[0] in {"runtime", ".venv", "node_modules", ".git"} or Path(name).name == ".env" for name in files):
        raise SystemExit("Runtime or credentials must not be tracked.")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    prefix = "BOSS_RECRUIT_AGENT_V0_1/"
    manifest = {"version": "0.1.0", "source_commit": commit,
                "packaged_at": datetime.now(timezone.utc).isoformat(), "files": []}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recruit-release-") as temporary:
        bundle = Path(temporary) / "SOURCE_HISTORY.bundle"
        subprocess.run(["git", "bundle", "create", str(bundle), "--all"], cwd=ROOT, check=True, capture_output=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, path in [(name, ROOT / name) for name in files] + [(bundle.name, bundle)]:
                data = path.read_bytes()
                archive.write(path, prefix + name)
                manifest["files"].append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            archive.writestr(prefix + "RELEASE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise SystemExit("ZIP integrity check failed.")
    print(json.dumps({"path": str(destination), "source_commit": commit,
                      "file_count": len(files) + 2, "bytes": destination.stat().st_size,
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
