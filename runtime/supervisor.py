"""PM-Quant Supervisor & Process Management.

Manages background services, health monitoring, PID tracking, and revision sealing.
"""
import sys
import json
import time
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

PID_FILE = config.RUNTIME_DIR / "services.pid.json"


def get_status() -> dict:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def seal_batch():
    """Seal current revision: package code snapshot, generate manifest.json, set to sealed."""
    ledger = config.CURRENT_LEDGER_DIR
    ledger.mkdir(parents=True, exist_ok=True)
    
    # 1. Zip snapshot of core/ and config.py
    snapshot_zip = ledger / "code_snapshot.zip"
    with zipfile.ZipFile(snapshot_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(config.ROOT / "config.py", "config.py")
        for f in (config.ROOT / "core").glob("*.py"):
            zf.write(f, f"core/{f.name}")
            
    # 2. Write manifest.json
    manifest = {
        "batch_id": config.BATCH_ID,
        "version": config.VERSION,
        "revision": config.REVISION,
        "sealed_at": time.time(),
        "params": config.PARAMS,
        "code_snapshot": str(snapshot_zip.relative_to(config.ROOT))
    }
    (ledger / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Batch {config.BATCH_ID} successfully sealed and archived.")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "seal":
        seal_batch()
    elif cmd == "status":
        st = get_status()
        print(f"PM-Quant Status ({config.BATCH_ID}):")
        if not st:
            print("  No active services registered.")
        else:
            for k, v in st.items():
                print(f"  {k:12s}: PID {v.get('pid')} (started {v.get('started')})")
    elif cmd == "help":
        print("Usage: python runtime/supervisor.py [status|seal|help]")


if __name__ == "__main__":
    main()
