"""PM-Quant Master Entry Point & Command Line Interface.

Usage:
  python main.py [start] [port] [--no-browser]
                           - 一键启动量化交易管道 + Web看板 (+ 浏览器)
  python main.py stop      - 一键停止所有交易进程与释放 8900 端口
  python main.py run       - 启动纸面（Paper）量化做市与对冲管道 (Pipeline Driver)
  python main.py web [p] [--no-browser]
                           - 仅启动 Web 投资人监控看板 (默认端口: 8900)
  python main.py test      - 运行所有微观结构与数学不变量单元测试
  python main.py reset     - 重置当前版本账本为干净初始状态 ($100.00)
  python main.py replay    - 回放历史微观订单流录制数据
  python main.py stress [N] [seed] - 合成行情压力测试(15种场景×N条路径+敏感性+连续序列)
  python main.py seal      - 归档封存当前实验批次代码与快照
  python main.py status    - 查看当前系统版本身份与核心经济学参数
"""
import sys
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config


def cmd_start(port: int = 8900, open_browser: bool = True):
    """Start all-in-one system: web dashboard server + trading pipeline + browser."""
    import time
    import threading
    import webbrowser
    from web.server import start_server_in_background
    from runtime.pipeline import PipelineDriver

    print("==================================================================")
    print(f"        PM-Quant 全自动量化做市与高频对冲系统 ({config.VERSION})        ")
    print("==================================================================")
    print(f"[*] 启动看板 Web 服务: http://127.0.0.1:{port}/#market")

    httpd = start_server_in_background(port)

    if open_browser:
        def _launch():
            time.sleep(1.0)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}/#market")
            except Exception:
                pass
        threading.Thread(target=_launch, daemon=True).start()

    print("[*] 启动 Paper 交易与对冲管道 (Pipeline Driver, no live order adapter)...")
    print("[*] 提示: 按 Ctrl+C 可一键平稳退出所有交易与看板服务。\n")
    try:
        driver = PipelineDriver()
        driver.run()
    except KeyboardInterrupt:
        print("\n\n[PM-Quant] 收到终止信号，正在关闭系统...")
    finally:
        if httpd:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        print("[PM-Quant] 所有交易管道与看板服务已安全退出。")


def cmd_stop():
    """Stop all running PM-Quant background processes and release port 8900."""
    import os
    import signal
    import subprocess
    from runtime.pipeline import is_pid_running, PID_FILE

    stopped_count = 0
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(pid):
                print(f"[*] 正在停止 Pipeline 交易进程 (PID: {pid})...")
                if sys.platform == "win32":
                    os.system(f"taskkill /F /PID {pid} >nul 2>&1")
                else:
                    os.kill(pid, signal.SIGTERM)
                stopped_count += 1
            PID_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"[!] 清理 PID 文件失败: {e}")

    # Check port 8900
    if sys.platform == "win32":
        try:
            out = subprocess.check_output('netstat -ano | findstr :8900', shell=True).decode('utf-8', errors='ignore')
            for line in out.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    port_pid = parts[-1]
                    print(f"[*] 正在关闭占用 8900 端口的看板进程 (PID: {port_pid})...")
                    os.system(f"taskkill /F /PID {port_pid} >nul 2>&1")
                    stopped_count += 1
        except Exception:
            pass

    if stopped_count > 0:
        print(f"[PM-Quant] 成功停止了 {stopped_count} 个服务进程。")
    else:
        print("[PM-Quant] 当前没有检测到正在运行的交易或看板进程。")


def cmd_run():
    from runtime.pipeline import PipelineDriver
    driver = PipelineDriver()
    driver.run()


def cmd_web(port: int = 8900, open_browser: bool = True):
    import threading
    import time
    import webbrowser
    from web.server import run

    if open_browser:
        def _launch():
            time.sleep(0.8)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}/#market")
            except Exception:
                pass
        threading.Thread(target=_launch, daemon=True).start()
    run(port)


def cmd_test():
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def cmd_reset(auto_stop: bool = True):
    """Reset current version experiment ledger to clean starting state with automatic backup."""
    import time
    from runtime.pipeline import is_pid_running, PID_FILE

    # 1. Stop running pipeline if active
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if is_pid_running(pid):
                if auto_stop:
                    print(f"[*] 检测到交易管道正在运行 (PID: {pid})，正在平稳停止以执行数据重置...")
                    cmd_stop()
                    time.sleep(1.0)
                else:
                    print(f"[Reset] Cannot reset while pipeline (PID {pid}) is running! Stop it first.")
                    return
        except Exception:
            pass

    ledger = config.CURRENT_LEDGER_DIR
    if not ledger.exists():
        ledger.mkdir(parents=True, exist_ok=True)

    # 2. Automatically backup existing ledger to archive directory
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = config.LEDGERS_DIR / f"{config.VERSION}_archive_{ts}"
    count_cleared = 0
    try:
        if ledger.exists() and any(ledger.iterdir()):
            shutil.copytree(ledger, backup_dir, dirs_exist_ok=True)
            print(f"[*] 历史数据已自动完整归档至: {backup_dir.name}")
    except Exception as e:
        print(f"[!] 备份历史数据提醒: {e}")

    # 3. Clean files in current ledger
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

    # 4. Remove stale live state instead of hand-writing a second copy of the
    # live schema. runtime/runner._write_live is the only producer of
    # live.json; duplicating its 45 fields here let the two drift apart.
    live_file = ledger / "live.json"
    if live_file.exists():
        live_file.unlink()

    print(f"=== PM-Quant 数据与账本重置完成 ({config.VERSION}) ===")
    print(f"[*] 清理并重置了当前账本 {count_cleared} 个记录项。")
    print(f"[*] 账本初始状态已就绪: 可用本金 ${config.PARAMS.get('INITIAL_CAPITAL_USD', 100.0):.2f}，风控状态 NORMAL，连亏归零。")


def cmd_replay():
    from sim.replay import run_replay
    run_replay()


def cmd_stress():
    from sim.stress import run_stress
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    run_stress(n, seed)


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


def _get_desktop_paths():
    """Retrieve actual Windows desktop directory paths from registry and system defaults."""
    import os
    from pathlib import Path
    found = []

    # 1. Windows Registry (User Shell Folders) - Most authoritative
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Desktop")
        expanded = os.path.expandvars(val)
        p = Path(expanded)
        if p.exists() and p not in found:
            found.append(p)
    except Exception:
        pass

    # 2. Common redirection locations (e.g. D:\桌面, D:\Desktop)
    for candidate in [Path("D:/桌面"), Path("D:/Desktop")]:
        if candidate.exists() and candidate not in found:
            found.append(candidate)

    # 3. User profile default
    up = os.environ.get("USERPROFILE", "")
    if up:
        for candidate in [Path(up) / "Desktop", Path(up) / "桌面"]:
            if candidate.exists() and candidate not in found:
                found.append(candidate)

    return found


def cmd_shortcut():
    """Create Windows Desktop shortcuts for One-Click PM-Quant Launch and Stop."""
    import os
    from pathlib import Path

    desktops = _get_desktop_paths()
    if not desktops:
        print("[!] 未检测到有效的 Windows 桌面路径。")
        return

    start_bat = ROOT / "启动系统.bat"
    stop_bat = ROOT / "停止系统.bat"

    success_paths = []
    for desktop in desktops:
        s1_path = desktop / "PM-Quant 一键启动.lnk"
        s2_path = desktop / "PM-Quant 一键停止.lnk"
        vbs_content = f"""
Set ws = CreateObject("WScript.Shell")
Set s1 = ws.CreateShortcut("{s1_path}")
s1.TargetPath = "{start_bat}"
s1.WorkingDirectory = "{ROOT}"
s1.WindowStyle = 1
s1.Description = "PM-Quant 全自动量化做市与高频对冲系统"
s1.Save()

Set s2 = ws.CreateShortcut("{s2_path}")
s2.TargetPath = "{stop_bat}"
s2.WorkingDirectory = "{ROOT}"
s2.WindowStyle = 1
s2.Description = "停止 PM-Quant 交易管道与看板"
s2.Save()
"""
        vbs_path = ROOT / "_temp_create_shortcut.vbs"
        try:
            vbs_path.write_text(vbs_content, encoding="gbk")
            os.system(f'cscript //nologo "{vbs_path}"')
            success_paths.append((s1_path, s2_path))
        except Exception as e:
            print(f"[!] 在 {desktop} 创建快捷方式失败: {e}")
        finally:
            vbs_path.unlink(missing_ok=True)

    if success_paths:
        print("[*] 成功在以下桌面位置创建快捷方式:")
        for s1, s2 in success_paths:
            print(f"    - {s1}")
            print(f"    - {s2}")


def _parse_port_flags(args, default_port: int = 8900):
    """Parse `[port] [--no-browser]` without exploding on unknown flags.

    The old parser did `int(sys.argv[2])`, so `start --no-browser` (or any
    mistyped argument) died with a bare ValueError traceback instead of telling
    the user what was wrong.
    """
    port = default_port
    open_browser = True
    for arg in args:
        low = arg.lower()
        if low in ("--no-browser", "-n"):
            open_browser = False
        elif low.startswith("--port="):
            port = _parse_port(low.split("=", 1)[1])
        elif arg.isdigit():
            port = _parse_port(arg)
        elif low in ("--browser", "-b"):
            open_browser = True
        else:
            print(f"[ERROR] Unknown option: {arg}\n")
            print(__doc__)
            sys.exit(2)
    return port, open_browser


def _parse_port(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not (1 <= port <= 65535):
        print(f"[ERROR] Invalid port: {raw!r} (expected 1-65535)\n")
        print(__doc__)
        sys.exit(2)
    return port


def main():
    if len(sys.argv) < 2:
        cmd_start()
        return

    cmd = sys.argv[1].lower()
    rest = sys.argv[2:]
    if cmd in ("start", "all"):
        port, open_browser = _parse_port_flags(rest)
        cmd_start(port, open_browser=open_browser)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "shortcut":
        cmd_shortcut()
    elif cmd == "run":
        cmd_run()
    elif cmd == "web":
        port, open_browser = _parse_port_flags(rest)
        cmd_web(port, open_browser=open_browser)
    elif cmd == "test":
        cmd_test()
    elif cmd == "reset":
        cmd_reset()
    elif cmd == "replay":
        cmd_replay()
    elif cmd == "stress":
        cmd_stress()
    elif cmd == "seal":
        cmd_seal()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: '{cmd}'")
        print(__doc__)


if __name__ == "__main__":
    main()
