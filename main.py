"""PM-Quant Master Entry Point & Command Line Interface.

Usage:
  python main.py run       - Start continuous live paper trading pipeline
  python main.py web [p]   - Start web dashboard server (default port: 8900)
  python main.py test      - Run all mathematical & dashboard unit tests
  python main.py replay    - Replay recorded historical market streams
  python main.py seal      - Seal current version with code snapshot & manifest
  python main.py status    - Show current version identity and parameters
"""
import sys
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
    print("\nCore Mathematical & Economic Parameters (Strictly 10):")
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
