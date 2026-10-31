"""SolSnipBot launcher.

Usage:
    python run.py [--host 127.0.0.1] [--port 8787]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BANNER = r"""
   ____       _   ____       _  ____        _
  / ___|  ___| | / ___| _ __ (_)| __ )  ___| |_
  \___ \ / _ \ | \___ \| '_ \| ||  _ \ / _ \ __|
   ___) |  __/ |  ___) | | | | || |_) | (_) | |_
  |____/ \___|_| |____/|_| |_|_||____/ \___/ \__|

  Solana Sniper Trading Bot  ·  v1.0.0
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="SolSnipBot server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if sys.platform.startswith("win"):
        # ProactorEventLoop is required for asyncio subprocess support on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    import uvicorn

    print(BANNER)
    print(f"  Dashboard : http://{args.host}:{args.port}")
    print("  Arrêt     : Ctrl+C\n")

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
