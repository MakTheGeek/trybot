"""Filesystem paths and global configuration for SolSnipBot."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Project root = parent of the backend package directory.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"

DATA_DIR.mkdir(parents=True, exist_ok=True)
(FRONTEND_DIR / "vendor").mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
CALLS_FILE = DATA_DIR / "calls.json"
LOGS_FILE = DATA_DIR / "logs.json"
STATS_FILE = DATA_DIR / "stats.json"
TELEGRAM_SESSION = DATA_DIR / "telegram.session"

# GMGN global credential file (same location the gmgn-cli uses).
GMGN_ENV = Path(os.path.expanduser("~")) / ".config" / "gmgn" / ".env"

# Secrets entered from the dashboard. Deliberately *outside* the project, next
# to the wallet key an earlier version already used: copying, zipping or sharing
# the bot folder then can never carry a private key along with it.
SECRETS_DIR = Path(os.path.expanduser("~")) / ".config" / "solsnipbot"
SECRETS_FILE = SECRETS_DIR / "secrets.env"


def _which_node() -> str:
    exe = shutil.which("node")
    if exe:
        return exe
    for candidate in (
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "node.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return "node"


def find_gmgn_cli_js() -> Path | None:
    """Locate the gmgn-cli entry point (dist/index.js) on this machine."""
    override = os.environ.get("GMGN_CLI_JS")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "gmgn-cli" / "dist" / "index.js")
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "nodejs" / "node_modules" / "gmgn-cli" / "dist" / "index.js")
    # Local project install fallback.
    candidates.append(ROOT / "node_modules" / "gmgn-cli" / "dist" / "index.js")
    for c in candidates:
        if c and c.exists():
            return c
    return None


NODE_EXE = _which_node()
GMGN_CLI_JS = find_gmgn_cli_js()

# Chain used by the sniper (Solana only for this app).
CHAIN = "sol"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

IS_WINDOWS = sys.platform.startswith("win")
