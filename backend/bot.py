"""Pipeline orchestration: Telegram call -> GMGN analysis -> snipe decision -> execution.

Calls are funnelled through a single serial worker queue so the bot never
floods the GMGN API (which bans IPs on repeated rate-limit violations). Frequent
duplicate calls are collapsed, and analyses that hit a rate limit are requeued.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from . import alertfilter
from . import config
from .analyzer import analyze
from .clock import clock
from .dexscreener import dexscreener
from .gmgn import GMGNError, GMGNRateLimited, gmgn
from .pricefeed import price_feed
from .state import store
from .telegram_listener import TelegramListener, get_listener
from .trader import get_sol_price, trader

DEDUPE_SECONDS = 1800     # ignore the same contract for 30 minutes
QUEUE_MAX = 120


class Bot:
    def __init__(self) -> None:
        self.listener: TelegramListener | None = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._recent: dict[str, float] = {}
        # Contrats dont un snipe manuel est **en cours**. Le clic est un
        # contournement de la file d'analyse : deux clics rapprochés lançaient
        # deux analyses en parallèle sur le même contrat, et les deux pouvaient
        # passer le contrôle « un seul achat par token » avant que le premier
        # achat soit enregistré. Refuser le second clic ferme cette course.
        self._manual_inflight: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self.processed = 0
        self.skipped = 0

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        # 1) clock calibration (GMGN validates the signed timestamp within +/-5s)
        await clock.sync()
        store.runtime["clock_drift"] = round(clock.drift_seconds, 1)
        store.log(
            f"Horloge calibrée (dérive {clock.drift_seconds:+.1f}s via {clock.source or 'local'})",
            level="success" if clock.measured else "warn",
            action="system",
        )

        # 2) GMGN + wallet + SOL price
        store.runtime["gmgn"] = "ok" if gmgn.available else "missing"
        if gmgn.available:
            await trader.refresh_wallet()
            await get_sol_price()
            if trader.wallet:
                store.log(
                    f"GMGN connecté — wallet {trader.wallet[:6]}…{trader.wallet[-4:]} "
                    f"({trader.sol_balance:.4f} SOL)",
                    level="success", action="gmgn",
                )
            else:
                store.log("GMGN connecté (aucun wallet lié à la clé API)",
                          level="warn", action="gmgn")
        else:
            store.log("gmgn-cli introuvable — lancez: npm install -g gmgn-cli",
                      level="error", action="gmgn")

        # 2b) Can this GMGN plan trade at all? Fund-free `order quote` probe, run
        # in the background so it never delays the listener. It surfaces the
        # blocking 403 PLAN_UPGRADE_REQUIRED in the UI right away instead of on
        # the next real snipe.
        if gmgn.available and trader.wallet:
            self._tasks.append(asyncio.create_task(trader.probe_trading_plan()))

        # 3) realtime position manager
        trader.start()

        # 3a) continuous Dexscreener tracking: the single poll loop that feeds
        # the manager's exits, the dashboard and the live chart.
        price_feed.start()

        # 3b) any token held without a position record (a swap whose bookkeeping
        # failed) must be adopted, not abandoned: background, never blocks start.
        self._tasks.append(asyncio.create_task(self._adopt_orphans_later()))

        # 4) serial analysis worker
        self._tasks.append(asyncio.create_task(self._worker()))

        # 5) telegram listener
        self.listener = get_listener(self.handle_call)
        self._tasks.append(asyncio.create_task(self.listener.start()))

        # 6) periodic housekeeping
        self._tasks.append(asyncio.create_task(self._housekeeping()))
        store.broadcast({"type": "status"})

    async def _adopt_orphans_later(self) -> None:
        """Let the RPC warm up, then adopt untracked balances."""
        try:
            await asyncio.sleep(8.0)
            await trader.adopt_orphans()
        except Exception as exc:  # noqa: BLE001
            store.log(f"Récupération des positions impossible: {str(exc)[:120]}",
                      level="warn", action="recover")

    async def stop(self) -> None:
        await trader.stop()
        await price_feed.stop()
        if self.listener:
            await self.listener.stop()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._started = False

    async def _housekeeping(self) -> None:
        tick = 0
        while True:
            try:
                await asyncio.sleep(30)
                tick += 1
                await get_sol_price()
                if tick % 2 == 0:            # refresh wallet every 60s
                    await trader.refresh_wallet_balance()
                self._prune_recent()
                store.recompute_stats()
                store.runtime["queue"] = self.queue.qsize()
                store.runtime["gmgn_cooling"] = gmgn.cooling_down
                store.broadcast({"type": "status"})
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                continue

    def _prune_recent(self) -> None:
        now = time.time()
        for addr in [a for a, t in self._recent.items() if now - t > DEDUPE_SECONDS]:
            self._recent.pop(addr, None)

    # ---------------------------------------------------------------- intake
    async def handle_call(self, address: str, message: str = "", channel: str = "",
                          source: str = "telegram") -> dict:
        """Enqueue a call for analysis (returns immediately)."""
        now = time.time()
        if now - self._recent.get(address, 0) < DEDUPE_SECONDS:
            self.skipped += 1
            return {"queued": False, "reason": "duplicate"}
        self._recent[address] = now

        # « Un seul achat par token » — enforced here as well as just before the
        # buy, so a contract already traded never costs a GMGN request. That
        # 30-minute de-duplication above is only a memory: it forgets on restart,
        # while a recorded position does not. No source is exempt, a manual snipe
        # included: the rule is about the contract, not about who asked.
        if config.all().get("snipe_once_per_token", True):
            deja = store.find_position_by_address(address)
            if deja is not None:
                self.skipped += 1
                store.log(
                    f"⏭️ {deja.get('symbol') or address[:8]} déjà traité "
                    f"({'position ouverte' if deja.get('status') == 'open' else 'trade clôturé'})"
                    " — un seul achat par token, appel ignoré",
                    level="info", token=address, action="once",
                )
                return {"queued": False, "reason": "already-traded"}

        job = {
            "address": address,
            "message": message,
            "channel": channel,
            "source": source,
            "attempts": 0,
            "ts": now,
        }

        # Stage 0 — the alert's own numbers. A clear violation is rejected here,
        # in microseconds and with **zero** GMGN requests, so the analysis queue
        # only ever holds tokens that could actually become a buy.
        settings = config.all()
        if settings.get("alert_prefilter", True) and message:
            verdict = alertfilter.prefilter(message, settings)
            if verdict["reject"]:
                self.skipped += 1
                return self._record_prefilter(job, verdict)

        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.skipped += 1
            store.log(
                f"File d'analyse saturée ({QUEUE_MAX}) — {address[:8]}… ignoré",
                level="warn", token=address, action="queue",
            )
            return {"queued": False, "reason": "queue_full"}

        store.runtime["queue"] = self.queue.qsize()
        return {"queued": True}

    def _record_prefilter(self, job: dict, verdict: dict) -> dict:
        """Store a rejection decided from the alert text alone (no GMGN call)."""
        report = alertfilter.build_report(job, verdict)
        reason = "; ".join(report["reasons"]) or "critères non remplis"
        store.log(
            f"⛔ {report['symbol']} rejeté (pré-filtre alerte) — {reason}",
            level="warn", token=job["address"], action="rejected",
        )
        store.add_call(report)
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return {"queued": False, "reason": "prefilter", "report": report}

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process(job)
            except GMGNRateLimited as exc:
                if job["attempts"] < 2:
                    job["attempts"] += 1
                    await asyncio.sleep(20)
                    with_room = True
                    try:
                        self.queue.put_nowait(job)
                    except asyncio.QueueFull:
                        with_room = False
                    store.log(
                        f"GMGN temporairement limité — {job['address'][:8]}… remis en file"
                        if with_room else
                        f"GMGN temporairement limité — {job['address'][:8]}… abandonné",
                        level="warn", token=job["address"], action="rate_limit",
                    )
                else:
                    store.log(
                        f"GMGN rate limit persistant — analyse de {job['address'][:8]}… abandonnée",
                        level="error", token=job["address"], action="rate_limit",
                    )
            except Exception as exc:  # noqa: BLE001
                store.log(f"Erreur du worker: {exc}", level="error", action="worker")
            finally:
                self.queue.task_done()
                store.runtime["queue"] = self.queue.qsize()

    # --------------------------------------------------------------- pipeline
    async def _process(self, job: dict) -> dict:
        address = job["address"]

        # An alert can age past the ceiling while it waits its turn. In that case
        # the GMGN request would only confirm the rejection, so skip it.
        settings = config.all()
        elapsed = max(time.time() - float(job.get("ts") or time.time()), 0.0)
        if settings.get("alert_prefilter", True) and job.get("message"):
            verdict = alertfilter.prefilter(job["message"], settings, elapsed=elapsed)
            if verdict["reject"]:
                return self._record_prefilter(job, verdict)

        call_id = "call_" + uuid.uuid4().hex[:10]
        store.log(f"Analyse GMGN de {address[:8]}…", level="info", token=address, action="analyze")
        try:
            report = await analyze(
                address, source=job["source"], message=job["message"], channel=job["channel"]
            )
        except GMGNRateLimited:
            raise
        except Exception as exc:  # noqa: BLE001
            store.log(f"Analyse impossible pour {address[:8]}…: {exc}",
                      level="error", token=address, action="analyze")
            report = {
                "address": address, "symbol": "?", "name": "?", "logo": "",
                "decision": "rejected", "reasons": [f"Erreur d'analyse: {exc}"],
                "failed_ids": ["error"], "checks": [], "source": job["source"],
                "channel": job["channel"], "message": job["message"][:400],
                "ts": int(time.time() * 1000), "market_cap": 0, "volume_24h": 0,
                "liquidity": 0, "top10_pct": 0, "buyers": 0, "sellers": 0,
                "age_minutes": 0, "holders": 0, "kols": 0, "smart_wallets": 0,
                "website": "", "socials": [], "price_usd": 0, "price_at_call": 0,
                "error": True,
            }

        report["id"] = call_id
        report["executed"] = False
        report["execution"] = None
        self.processed += 1

        # A manual snipe is an explicit instruction from the user: it needs the
        # master switch, but not the automatic-sniper toggle.
        manual = job.get("source") == "manual"
        # Paper mode replaces real trading instead of adding to it: nothing is ever
        # signed or broadcast, so `live_trading` stops deciding anything. The flag
        # is written on the call itself, which is what keeps a paper test out of
        # the real statistics.
        paper = bool(settings.get("paper_trading"))
        report["paper"] = paper
        # A token that has already been bought is not bought again — the position
        # list is the source of truth, so a closed trade still blocks a re-entry
        # and the rule survives a restart. A manual snipe is not exempt from this
        # one: it is an explicit instruction, which is precisely why it must not
        # quietly double a position either. Its only privilege is `auto_snipe`.
        deja = None
        if settings.get("snipe_once_per_token", True):
            deja = store.find_position_by_address(address)
        will_trade = (
            report.get("decision") == "sniped"
            and (settings.get("live_trading", True) or paper)
            and (manual or settings.get("auto_snipe"))
            and not report.get("error")
            and deja is None
        )

        if report.get("decision") == "sniped" and deja is not None:
            # The criteria passed, but this contract has already been bought once.
            store.log(
                f"⏭️ {report.get('symbol')} déjà traité "
                f"({'position ouverte' if deja.get('status') == 'open' else 'trade clôturé'})"
                " — un seul achat par token, aucun second achat",
                level="info", token=address, action="once",
            )
        elif report.get("decision") == "sniped":
            store.log(
                f"{'📝 ' if paper else '✅ '}{report.get('symbol')} validé — "
                f"MC ${report.get('market_cap', 0):,.0f} · "
                f"Top10 {report.get('top10_pct', 0):.1f}% · âge {report.get('age_minutes', 0):.1f}m"
                + (" · mode papier" if paper else ""),
                level="success", token=address, action="sniped",
            )
        else:
            reason = "; ".join(report.get("reasons") or []) or "critères non remplis"
            store.log(f"⛔ {report.get('symbol')} rejeté — {reason}",
                      level="warn", token=address, action="rejected")

        if will_trade:
            # The limit is per mode: a paper test must not be throttled by a real
            # position (and vice versa).
            if len(store.open_positions(paper=paper)) >= int(settings.get("max_concurrent_positions", 5)):
                report["execution"] = {"executed": False, "paper": paper,
                                       "reason": "Nombre maximum de positions simultanées atteint."}
                store.log("Snipe ignoré: limite de positions atteinte",
                          level="warn", token=address, action="buy")
            else:
                result = await trader.buy(report)
                report["executed"] = bool(result.get("executed"))
                report["execution"] = result
                report["position_id"] = result.get("position_id", "")
                if not result.get("executed"):
                    store.log(f"Snipe non exécuté: {result.get('reason')}",
                              level="warn", token=address, action="buy")
        elif report.get("decision") == "sniped":
            if deja is not None:
                why = "Token déjà traité — un seul achat par token."
            elif not (manual or settings.get("auto_snipe")):
                why = "Sniper automatique désactivé — analyse seulement."
            elif paper:
                why = "Mode papier actif — aucun ordre réel."
            else:
                why = "Trading réel désactivé dans les paramètres."
            report["execution"] = {"executed": False, "paper": paper, "reason": why}

        store.add_call(report)
        store.recompute_stats()
        store.broadcast({"type": "status"})
        return report

    # ------------------------------------------------------------- manual ops
    async def force_snipe(self, address: str) -> dict:
        """Analyse (and possibly snipe) a contract immediately, bypassing the queue.

        A manual snipe keeps one privilege — it needs ``live_trading``, not
        ``auto_snipe`` — and no other. In particular it obeys « un seul achat par
        token » like every other call: a contract already traded is refused here,
        before the analysis, so the refusal costs no GMGN request. That refusal is
        the same one a Telegram call gets, and it is worth being able to read it:
        wanting to re-enter a token is a common, legitimate impulse, and the
        answer is a sentence, not a silent no-op.

        The click also skips the queue, so two of them would run concurrently and
        both could clear the rule before the first buy is recorded. A second click
        on a contract already being processed is therefore refused as well.
        """
        now = time.time()
        if address in self._manual_inflight:
            reason = "Snipe manuel déjà en cours pour ce contrat."
            return {"queued": False, "reason": "in-progress",
                    "decision": "in-progress", "reasons": [reason],
                    "executed": False,
                    "execution": {"executed": False, "reason": reason}}
        self._manual_inflight.add(address)
        try:
            self._recent[address] = now
            if config.all().get("snipe_once_per_token", True):
                deja = store.find_position_by_address(address)
                if deja is not None:
                    reason = (
                        f"{deja.get('symbol') or address[:8]} déjà traité "
                        f"({'position ouverte' if deja.get('status') == 'open' else 'trade clôturé'})"
                        " — un seul achat par token, snipe manuel refusé"
                    )
                    store.log(f"⏭️ {reason}", level="info", token=address, action="once")
                    return {"queued": False, "reason": "already-traded",
                            "decision": "already-traded", "reasons": [reason],
                            "executed": False,
                            "execution": {"executed": False, "reason": reason}}
            job = {"address": address, "message": "(snipe manuel)", "channel": "manuel",
                   "source": "manual", "attempts": 0, "ts": now}
            return await self._process(job)
        finally:
            self._manual_inflight.discard(address)

    async def close_position(self, position_id: str, percent: float = 100.0) -> dict:
        """Sell part or all of one position, on the user's explicit click.

        Closing is a sale, so the guard rails are the trader's: a paper position
        is always closed virtually and a real one always sold for real, whichever
        mode is on. What is decided here is only the shape of the refusal — a
        trade that is already closed has no amount left to sell, and saying so is
        more use than letting it reach the quote and come back with "the amount
        rounds to zero".
        """
        pos = store.get_position(position_id)
        if not pos:
            return {"ok": False, "error": "Position introuvable"}
        if pos.get("status") != "open":
            return {"ok": False, "executed": False,
                    "error": f"Position déjà fermée ({pos.get('exit_reason') or 'clôturée'}).",
                    "reason": f"Position déjà fermée ({pos.get('exit_reason') or 'clôturée'})."}
        result = await trader.sell(pos, percent=percent, reason="manual", manual=True)
        return {"ok": bool(result.get("executed")), **result}


bot = Bot()
