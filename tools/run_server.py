"""命令行启动封账后端服务。

用法：python3 tools/run_server.py [--host 127.0.0.1] [--port 8080] [--db report_closing.db]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from report_closing.__main__ import main

if __name__ == "__main__":
    main()
