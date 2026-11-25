"""PM-Quant Version Sealing.

Packages reproducible code snapshots and parameter manifests for ledger archiving.
"""
import sys
import json
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config


def seal_batch():
    """Seal current version: package code snapshot, generate manifest.json."""
    ledger = config.CURRENT_LEDGER_DIR
    ledger.mkdir(parents=True, exist_ok=True)

    # 1. Zip snapshot of every module that can change the ledger's behaviour.
    #    A sealed version must be reconstructable: packaging only core/ hid the
    #    matching engine, the replay engine and the dashboard behind an
    #    incomplete snapshot.
    snapshot_zip = ledger / "code_snapshot.zip"
    files = [(config.ROOT / "main.py", "main.py"), (config.ROOT / "config.py", "config.py")]
    for pkg in ("core", "runtime", "sim", "web"):
        pkg_dir = config.ROOT / pkg
        if not pkg_dir.exists():
            continue
        for f in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            files.append((f, f.relative_to(config.ROOT).as_posix()))
    with zipfile.ZipFile(snapshot_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in files:
            zf.write(path, arcname)

    # 2. Write manifest.json
    manifest = {
        "version": config.VERSION,
        "sealed_at": time.time(),
        "params": config.PARAMS,
        "code_snapshot": str(snapshot_zip.relative_to(config.ROOT)),
        "files": [arcname for _path, arcname in files],
    }
    (ledger / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Version {config.VERSION} successfully sealed and archived.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "seal":
        seal_batch()
    else:
        print("Usage: python runtime/supervisor.py [seal|help]")


if __name__ == "__main__":
    main()
