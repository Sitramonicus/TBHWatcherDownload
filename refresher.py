from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from watcher_config import logInfo, logError


class Refresher:
    def __init__(self, configProvider: Callable, state, stopEvent: threading.Event,
                 booter, reloadConfig: Optional[Callable] = None) -> None:
        self.configProvider = configProvider
        self.state = state
        self.stopEvent = stopEvent
        self.booter = booter
        self.reloadConfig = reloadConfig or (lambda: None)
        self.gameStartedAt: Optional[float] = None
        self.jitterOffset: float = 0.0
        self.forcedTargetSeconds: Optional[float] = None
        self.inCooldown: bool = False

    def config(self, key, default=None):
        return getattr(self.configProvider(), key, default)

    def intervalSeconds(self) -> float:
        return self.config("refresher_interval_minutes", 7.0) * 60

    def newJitterOffset(self) -> float:
        jitterMax = self.config("refresher_interval_jitter_seconds", 20.0)
        return random.uniform(-jitterMax, jitterMax)

    def effectiveTargetSeconds(self) -> float:
        if self.forcedTargetSeconds is not None:
            return self.forcedTargetSeconds
        return max(5.0, self.intervalSeconds() + self.jitterOffset)

    def cycleRunning(self) -> bool:
        return (self.gameStartedAt is not None
                and not self.inCooldown
                and bool(self.config("refresher_enabled", True)))

    def forceRefreshSoon(self, seconds: float = 10.0) -> tuple:
        if not self.config("refresher_enabled", True):
            return False, "Refresher is disabled"
        if self.inCooldown:
            return False, "Refresher is in cooldown"
        if self.gameStartedAt is None:
            return False, "No refresh cycle running (game not detected yet)"
        try:
            secondsValue = max(1.0, float(seconds))
        except Exception:
            secondsValue = 10.0
        self.forcedTargetSeconds = (time.time() - self.gameStartedAt) + secondsValue
        logInfo(f"[refresher] DEBUG: current cycle target forced so the next refresh "
                f"is in ~{secondsValue:.0f}s.")
        return True, f"Next refresh in ~{int(secondsValue)}s"


    def syncEnabledStatus(self, previous: bool) -> bool:
        enabled = self.config("refresher_enabled", True)
        if enabled != previous:
            self.state.setFields(refresherStatus="Waiting for game" if enabled else "Disabled")
        return enabled

    def runCooldown(self) -> None:
        self.inCooldown = True
        started = time.time()
        logInfo(f"[refresher] Cooldown for {self.config('refresher_cooldown_seconds', 90)}s.")
        while not self.stopEvent.is_set() and self.config("refresher_enabled", True):
            cooldown = self.config("refresher_cooldown_seconds", 90)
            remaining = int(cooldown - (time.time() - started))
            if remaining <= 0:
                break
            try:
                self.booter.markBusy(safetyClearAfter=max(15.0, remaining + 30.0))
            except Exception:
                pass
            self.state.setFields(currentTask="Refresher cooldown",
                                 gameState="Closed (cooldown)",
                                 refresherStatus=f"Cooldown ({remaining}s left)")
            time.sleep(1)
        self.inCooldown = False

    def doRefresh(self) -> None:
        self.forcedTargetSeconds = None
        logInfo(f"[refresher] Interval reached ({self.config('refresher_interval_minutes', 7.0)}m); refreshing.")
        self.state.setFields(currentTask="Refresher: closing game",
                             gameState="Refreshing",
                             refresherStatus="Closing game",
                             lastEvent="Periodic refresh triggered")
        self.booter.closeGame()
        self.runCooldown()
        if not self.stopEvent.is_set() and self.config("refresher_enabled", True):
            logInfo("[refresher] Cooldown done; relaunching game.")
            self.booter.launchGameFlow()

    def run(self) -> None:
        previousEnabled = self.config("refresher_enabled", True)
        while not self.stopEvent.is_set():
            time.sleep(1)
            try:
                self.reloadConfig()
                previousEnabled = self.syncEnabledStatus(previousEnabled)

                if not previousEnabled:
                    self.gameStartedAt = None
                    continue

                if not self.booter.isGameRunning():
                    if self.gameStartedAt is not None:
                        logInfo("[refresher] Game gone; timer reset.")
                    self.gameStartedAt = None
                    self.forcedTargetSeconds = None
                    self.state.setFields(refresherStatus="Waiting for game")
                    continue

                if self.gameStartedAt is None:
                    self.gameStartedAt = time.time()
                    self.jitterOffset = self.newJitterOffset()
                    logInfo(f"[refresher] Game detected; timer started "
                            f"(target {self.effectiveTargetSeconds():.0f}s this cycle).")
                    self.state.setFields(refresherStatus="Active (timer started)")

                target = self.effectiveTargetSeconds()
                elapsed = time.time() - self.gameStartedAt
                remaining = max(0, target - elapsed)
                self.state.setFields(
                    refresherStatus=f"Active ({int(remaining // 60)}m {int(remaining % 60)}s left)")

                if elapsed >= target:
                    self.doRefresh()
                    self.gameStartedAt = None
            except Exception as error:
                logError("Refresher loop tick error", error=error, where="refresher.run")
                self.state.setFields(refresherStatus="Error (see log)")

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        issues: list[str] = []
        try:
            seconds = self.intervalSeconds()
            report("refresher.interval", "PASS" if seconds > 0 else "FAIL", f"{seconds:.0f}s")
            if seconds <= 0:
                issues.append("interval not positive")
        except Exception as error:
            report("refresher.interval", "FAIL", str(error))
            issues.append("interval raised")

        try:
            enabled = self.config("refresher_enabled", True)
            report("refresher.enabled", "PASS", str(enabled))
        except Exception as error:
            report("refresher.enabled", "FAIL", str(error))
            issues.append("enabled flag raised")

        try:
            hasBooter = self.booter is not None and hasattr(self.booter, "launchGameFlow")
            report("refresher.booter_link", "PASS" if hasBooter else "FAIL",
                   "booter wired" if hasBooter else "booter missing")
            if not hasBooter:
                issues.append("booter not wired")
        except Exception as error:
            report("refresher.booter_link", "FAIL", str(error))
            issues.append("booter link raised")

        return issues
