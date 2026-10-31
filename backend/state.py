"""In-memory application state with JSON persistence and a tiny pub/sub bus."""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from .paths import CALLS_FILE, LOGS_FILE, POSITIONS_FILE, STATS_FILE

MAX_CALLS = 300
MAX_LOGS = 500


def _read(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return default


def _write(path, data) -> None:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.positions: list[dict] = _read(POSITIONS_FILE, []) or []
        self.calls: deque[dict] = deque(_read(CALLS_FILE, []) or [], maxlen=MAX_CALLS)
        self.logs: deque[dict] = deque(_read(LOGS_FILE, []) or [], maxlen=MAX_LOGS)
        self.stats: dict = _read(STATS_FILE, {}) or {}
        self._subscribers: list[Callable[[dict], None]] = []
        self.runtime: dict[str, Any] = {
            "telegram": "disconnected",
            "telegram_user": "",
            "telegram_channels": [],
            "gmgn": "unknown",
            "dexscreener": "ok",
            "clock_drift": 0.0,
            "wallet": "",
            "wallet_balance_sol": 0.0,
            "sol_price": 0.0,
            "trading_blocked": False,
            "started_at": int(time.time() * 1000),
        }

    # -------------------------------------------------------------- pub/sub
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]) -> None:
        if fn in self._subscribers:
            self._subscribers.remove(fn)

    def broadcast(self, event: dict) -> None:
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- position
    def new_position_id(self) -> str:
        return "pos_" + uuid.uuid4().hex[:12]

    def add_position(self, position: dict) -> dict:
        with self._lock:
            self.positions.insert(0, position)
            _write(POSITIONS_FILE, self.positions)
        self.broadcast({"type": "position_open", "position": position})
        return position

    def update_position(self, pos_id: str, patch: dict, *, persist: bool = True) -> dict | None:
        with self._lock:
            for pos in self.positions:
                if pos.get("id") == pos_id:
                    pos.update(patch)
                    if persist:
                        _write(POSITIONS_FILE, self.positions)
                    return pos
        return None

    def get_position(self, pos_id: str) -> dict | None:
        with self._lock:
            for pos in self.positions:
                if pos.get("id") == pos_id:
                    return pos
        return None

    def find_position_by_address(self, address: str) -> dict | None:
        """The first position ever recorded for this token, open **or** closed.

        This is what enforces « one purchase per contract »: the rule is about a
        token having been traded, not about a bag being held, so a closed position
        still counts. Reading the persisted list (rather than today's open
        positions) is what makes it survive a restart.
        """
        if not address:
            return None
        with self._lock:
            for pos in self.positions:
                if pos.get("address") == address:
                    return pos
        return None

    def open_positions(self, *, paper: bool | None = None) -> list[dict]:
        """Open positions. ``paper`` filters the mode: None = both, True = the
        virtual ones only, False = the real ones only.

        The mode is a property of each *position*, never of the current setting:
        a real bag must always keep being sold for real, and a paper position must
        always be closed virtually, even if the switch is flipped while it is open.
        """
        with self._lock:
            items = [p for p in self.positions if p.get("status") == "open"]
            if paper is not None:
                items = [p for p in items if bool(p.get("paper")) is paper]
            return list(items)

    def push_position_update(self, position: dict) -> None:
        self.broadcast({"type": "position_update", "position": position})

    # ----------------------------------------------------------------- calls
    def add_call(self, call: dict) -> dict:
        with self._lock:
            self.calls.appendleft(call)
            _write(CALLS_FILE, list(self.calls))
        self.broadcast({"type": "call", "call": call})
        return call

    def update_call(self, call_id: str, patch: dict) -> dict | None:
        with self._lock:
            for call in self.calls:
                if call.get("id") == call_id:
                    call.update(patch)
                    _write(CALLS_FILE, list(self.calls))
                    return call
        return None

    # ------------------------------------------------------------------ logs
    def log(self, message: str, *, level: str = "info", token: str = "", action: str = "") -> dict:
        entry = {
            "ts": int(time.time() * 1000),
            "level": level,
            "message": message,
            "token": token,
            "action": action,
        }
        with self._lock:
            self.logs.appendleft(entry)
            _write(LOGS_FILE, list(self.logs))
        self.broadcast({"type": "log", "log": entry})
        return entry

    # ----------------------------------------------------------------- stats
    def _bucket(self, paper: bool) -> dict:
        """Stats of one mode. Real trades only in the headline, ever.

        Paper results are a test: mixing them into the headline would make a
        simulated P&L readable as a real one, which is the one thing this bot must
        never do. They live in ``stats["paper"]`` and are labelled as virtual.
        """
        closed = [p for p in self.positions
                  if p.get("status") == "closed" and bool(p.get("paper")) is paper]
        opened = [p for p in self.positions
                  if p.get("status") == "open" and bool(p.get("paper")) is paper]
        realized = sum(float(p.get("pnl_usd") or 0.0) for p in closed)
        unrealized = sum(float(p.get("pnl_usd") or 0.0) for p in opened)
        wins = [p for p in closed if float(p.get("pnl_usd") or 0.0) > 0]
        invested = sum(float(p.get("amount_sol") or 0.0) for p in closed)
        return {
            "total_pnl": round(realized + unrealized, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate": round((len(wins) / len(closed) * 100.0) if closed else 0.0, 1),
            "invested_sol": round(invested, 4),
            "open_count": len(opened),
        }

    def recompute_stats(self) -> dict:
        with self._lock:
            sniped = rejected = 0
            paper_sniped = paper_rejected = 0
            for call in self.calls:
                is_paper = bool(call.get("paper"))
                if call.get("decision") == "sniped":
                    if is_paper:
                        paper_sniped += 1
                    else:
                        sniped += 1
                elif call.get("decision") == "rejected":
                    if is_paper:
                        paper_rejected += 1
                    else:
                        rejected += 1
            self.stats = {
                **self._bucket(False),
                "sniped": sniped,
                "rejected": rejected,
                # A whole separate scoreboard, so a paper test can be read on its
                # own without ever being taken for a real result.
                "paper": {
                    **self._bucket(True),
                    "sniped": paper_sniped,
                    "rejected": paper_rejected,
                },
            }
            _write(STATS_FILE, self.stats)
            return dict(self.stats)

    # -------------------------------------------------------------- snapshot
    # Le plafond ne doit **jamais** masquer une position ouverte : le tableau de
    # bord est l'endroit où on la ferme à la main, et une position que le
    # navigateur ne reçoit pas ne peut pas être fermée. Les ouvertes passent donc
    # en premier, toutes, et le plafond ne rogne que l'historique des clôturées.
    POSITIONS_IN_SNAPSHOT = 60

    def snapshot(self) -> dict:
        with self._lock:
            open_pos = [p for p in self.positions if p.get("status") == "open"]
            closed = [p for p in self.positions if p.get("status") != "open"]
            room = max(0, self.POSITIONS_IN_SNAPSHOT - len(open_pos))
            return {
                "positions": open_pos + closed[:room],
                "calls": list(self.calls)[:80],
                "logs": list(self.logs)[:120],
                "stats": self.recompute_stats(),
                "runtime": dict(self.runtime),
            }


store = Store()
