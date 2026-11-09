"""PM-Quant Master Entry Point & Command Line Interface.

Usage:
  python main.py run       - Start continuous live paper trading pipeline
  python main.py web [p]   - Start web dashboard server (default port: 8900)
  python main.py test      - Run all mathematical & dashboard unit tests
  python main.py reset     - Reset current version experiment to clean starting slate
  python main.py replay    - Replay recorded historical market streams
  python main.py seal      - Seal current version with code snapshot & manifest
  python main.py status    - Show current version identity and parameters
"""
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config


def cmd_run():
    from runtime.pipeline import PipelineDriver
    driver = PipelineDriver()
    driver.run()


def cmd_web(port: int = 8900):
    from web.server import run
    run(port)


def cmd_test():
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def cmd_reset():
    """Reset current version experiment ledger to clean starting state."""
    from runtime.pipeline import is_pid_running, PID_FILE
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(pid):
                print(f"[Reset] Cannot reset while pipeline (PID {pid}) is running! Stop it first.")
                return
        except Exception:
            pass

    ledger = config.CURRENT_LEDGER_DIR
    count_cleared = 0
    if ledger.exists():
        for item in list(ledger.iterdir()):
            if item.name in ("manifest.json", "code_snapshot.zip"):
                continue  # preserve sealed archive if present
            if item.is_file():
                item.unlink()
                count_cleared += 1
            elif item.is_dir() and item.name == "windows":
                for sub in list(item.glob("*.gz")):
                    sub.unlink()
                    count_cleared += 1

    (ledger / "windows").mkdir(parents=True, exist_ok=True)
    print(f"=== PM-Quant Experiment Reset ({config.VERSION}) ===")
    print(f"Cleaned {count_cleared} ledger item(s) under: {ledger}")
    print("Ledger is now in pristine state: Starting Capital $100.00, 0 trades, 0 loss streak.")


def cmd_replay():
    from sim.replay import run_replay
    run_replay()


def cmd_seal():
    from runtime.supervisor import seal_batch
    seal_batch()


def cmd_status():
    print("=== PM-Quant Status ===")
    print(f"Version:      {config.VERSION}")
    print(f"Ledger Path:  {config.CURRENT_LEDGER_DIR}")
    print(f"\nCore Mathematical & Economic Parameters ({config.VERSION}):")
    for k, v in config.PARAMS.items():
        print(f"  {k:22s}: {v}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()
    if cmd == "run":
        cmd_run()
    elif cmd == "web":
        p = int(sys.argv[2]) if len(sys.argv) > 2 else 8900
        cmd_web(p)
    elif cmd == "test":
        cmd_test()
    elif cmd == "reset":
        cmd_reset()
    elif cmd == "replay":
        cmd_replay()
    elif cmd == "seal":
        cmd_seal()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: '{cmd}'")
        print(__doc__)


if __name__ == "__main__":
    main()
