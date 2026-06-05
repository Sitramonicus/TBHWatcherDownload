from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from watcher_config import log, log_error

SECONDS_PER_MINUTE = 60
MINIMUM_INTERVAL_SECONDS = 5.0
DEFAULT_INTERVAL_MINUTES = 7.0
DEFAULT_JITTER_SECONDS = 20.0
DEFAULT_COOLDOWN_SECONDS = 90
LOOP_TICK_SECONDS = 1
BUSY_SAFETY_MINIMUM_SECONDS = 15.0
BUSY_SAFETY_HEADROOM_SECONDS = 30.0


class Refresher:
    def __init__(
        self,
        config_provider: Callable[[], object],
        state,
        stop_event: threading.Event,
        booter,
        reload_config: Optional[Callable[[], None]] = None,
    ) -> None:
        self._read_config = config_provider
        self.state = state
        self.stop_event = stop_event
        self.booter = booter
        self._reload_config = reload_config or (lambda: None)
        self._game_first_seen_at: Optional[float] = None
        self._cycle_jitter_seconds: float = 0.0

    def _setting(self, key, default=None):
        return getattr(self._read_config(), key, default)

    def _base_interval_seconds(self) -> float:
        return self._setting("refresher_interval_minutes", DEFAULT_INTERVAL_MINUTES) * SECONDS_PER_MINUTE

    def _pick_cycle_jitter(self) -> float:
        spread = self._setting("refresher_interval_jitter_seconds", DEFAULT_JITTER_SECONDS)
        return random.uniform(-spread, spread)

    def _current_cycle_target_seconds(self) -> float:
        return max(MINIMUM_INTERVAL_SECONDS, self._base_interval_seconds() + self._cycle_jitter_seconds)

    def _announce_enabled_changes(self, was_enabled: bool) -> bool:
        is_enabled = self._setting("refresher_enabled", True)
        if is_enabled != was_enabled:
            self.state.set(refresher_status="Waiting for game" if is_enabled else "Disabled")
        return is_enabled

    def _wait_out_cooldown(self) -> None:
        started_at = time.time()
        log(f"[refresher] Cooldown for {self._setting('refresher_cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s.")
        while not self.stop_event.is_set() and self._setting("refresher_enabled", True):
            cooldown_seconds = self._setting("refresher_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
            seconds_left = int(cooldown_seconds - (time.time() - started_at))
            if seconds_left <= 0:
                break
            try:
                self.booter.mark_busy(
                    safety_clear_after=max(BUSY_SAFETY_MINIMUM_SECONDS, seconds_left + BUSY_SAFETY_HEADROOM_SECONDS)
                )
            except Exception:
                pass
            self.state.set(current_task="Refresher cooldown",
                           game_state="Closed (cooldown)",
                           refresher_status=f"Cooldown ({seconds_left}s left)")
            time.sleep(LOOP_TICK_SECONDS)

    def _close_wait_relaunch(self) -> None:
        log(f"[refresher] Interval reached "
            f"({self._setting('refresher_interval_minutes', DEFAULT_INTERVAL_MINUTES)}m); refreshing.")
        self.state.set(current_task="Refresher: closing game",
                       game_state="Refreshing",
                       refresher_status="Closing game",
                       last_event="Periodic refresh triggered")
        self.booter.close_game()
        self._wait_out_cooldown()
        if not self.stop_event.is_set() and self._setting("refresher_enabled", True):
            log("[refresher] Cooldown done; relaunching game.")
            self.booter.launch_game_flow()

    def run(self) -> None:
        was_enabled = self._setting("refresher_enabled", True)
        while not self.stop_event.is_set():
            time.sleep(LOOP_TICK_SECONDS)
            try:
                self._reload_config()
                was_enabled = self._announce_enabled_changes(was_enabled)

                if not was_enabled:
                    self._game_first_seen_at = None
                    continue

                if not self.booter.is_game_running():
                    if self._game_first_seen_at is not None:
                        log("[refresher] Game gone; timer reset.")
                    self._game_first_seen_at = None
                    self.state.set(refresher_status="Waiting for game")
                    continue

                if self._game_first_seen_at is None:
                    self._game_first_seen_at = time.time()
                    self._cycle_jitter_seconds = self._pick_cycle_jitter()
                    log(f"[refresher] Game detected; timer started "
                        f"(target {self._current_cycle_target_seconds():.0f}s this cycle).")
                    self.state.set(refresher_status="Active (timer started)")

                target_seconds = self._current_cycle_target_seconds()
                elapsed_seconds = time.time() - self._game_first_seen_at
                seconds_left = max(0, target_seconds - elapsed_seconds)
                minutes_part = int(seconds_left // SECONDS_PER_MINUTE)
                seconds_part = int(seconds_left % SECONDS_PER_MINUTE)
                self.state.set(refresher_status=f"Active ({minutes_part}m {seconds_part}s left)")

                if elapsed_seconds >= target_seconds:
                    self._close_wait_relaunch()
                    self._game_first_seen_at = None
            except Exception as error:
                log_error("Refresher loop tick error", exc=error, where="refresher.run")
                self.state.set(refresher_status="Error (see log)")

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        problems: list[str] = []
        try:
            interval = self._base_interval_seconds()
            report("refresher.interval", "PASS" if interval > 0 else "FAIL", f"{interval:.0f}s")
            if interval <= 0:
                problems.append("interval not positive")
        except Exception as error:
            report("refresher.interval", "FAIL", str(error))
            problems.append("interval raised")

        try:
            enabled = self._setting("refresher_enabled", True)
            report("refresher.enabled", "PASS", str(enabled))
        except Exception as error:
            report("refresher.enabled", "FAIL", str(error))
            problems.append("enabled flag raised")

        try:
            booter_wired = self.booter is not None and hasattr(self.booter, "launch_game_flow")
            report("refresher.booter_link", "PASS" if booter_wired else "FAIL",
                   "booter wired" if booter_wired else "booter missing")
            if not booter_wired:
                problems.append("booter not wired")
        except Exception as error:
            report("refresher.booter_link", "FAIL", str(error))
            problems.append("booter link raised")

        return problems
