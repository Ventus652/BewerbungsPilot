"""Start the local dashboard on http://127.0.0.1:8765 (Ctrl+C to stop).

Usage (from ``app``):  .\\.venv\\Scripts\\python.exe scripts\\serve.py [--port 8765] [--open]
The console process is a small supervisor: "Redémarrer l'interface" in the dashboard
restarts the server with the latest code without closing this window.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

RESTART_CODE = 75


def child(port: int) -> int:
    from bewerbungspilot.api.server import DashboardConfig, make_server

    config = DashboardConfig(ROOT, port=port)
    server = make_server(config)
    print(f"Interface BewerbungsPilot : http://127.0.0.1:{port}/  (Ctrl+C pour arrêter)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return RESTART_CODE if config.restart_requested else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BewerbungsPilot local dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args.port)
    opened = False
    while True:
        process = subprocess.Popen([sys.executable, __file__, "--child", "--port", str(args.port)], cwd=ROOT)
        if args.open and not opened:
            opened = True
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            return 0
        if code != RESTART_CODE:
            return code
        print("Redémarrage avec le code à jour…", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
