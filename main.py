from __future__ import annotations

import os
import sys
import threading
import time

import pystray

from watcher_config import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    LOG_FILE,
    WatchState,
    build_icon_image,
    confirm_dialog,
    acquire_single_instance,
    install_global_excepthook,
    load_config,
    warn_already_running,
    log,
    log_error,
    save_config,
    update_config_key,
)
import stealth_temp
import dll_mutate
from splash import Splash
from speedy_dll import CAMOUFLAGE_NAMES, SpeedyDLLManager
from boot import GameBooter
from refresher import Refresher

if sys.platform.startswith("win"):
    try:
        import pystray._win32  # noqa: F401
    except ImportError:
        pass

TRAY_ICON_ID = "TBHWatcher"
TRAY_ICON_TOOLTIP = "TBH Watcher"
STARTUP_PHASE_COUNT = 6
SECONDS_PER_MINUTE = 60
DEFAULT_INTERVAL_MINUTES = 7.0
READY_NOTIFICATION_DELAY_SECONDS = 1.5
QUICK_CLOSE_TIMEOUT_SECONDS = 2


class _NullSplash:
    def start(self) -> None:
        pass

    def step(self, phase_label: str) -> None:
        pass

    def message(self, text: str) -> None:
        pass

    def notify_ready(self, tray_icon=None) -> None:
        pass

    def finish(self) -> None:
        pass


class ConfigHolder:
    def __init__(self) -> None:
        self._last_mtime = 0.0
        self.raw: dict = {}
        self._lock = threading.Lock()
        for key, value in DEFAULT_CONFIG.items():
            setattr(self, key, value)
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        with self._lock:
            try:
                current_mtime = CONFIG_FILE.stat().st_mtime
            except OSError:
                if not CONFIG_FILE.exists():
                    save_config(DEFAULT_CONFIG)
                current_mtime = 1.0
            if not force and current_mtime == self._last_mtime:
                return
            self._last_mtime = current_mtime
            self.raw = load_config()
            for key, value in self.raw.items():
                setattr(self, key, value)
            self.refresher_interval_seconds = (
                self.raw.get("refresher_interval_minutes", DEFAULT_INTERVAL_MINUTES) * SECONDS_PER_MINUTE
            )
        log(f"[main] Config (re)loaded: interval={self.raw.get('refresher_interval_minutes')}m, "
            f"cooldown={self.raw.get('refresher_cooldown_seconds')}s.")


class TBHWatcher:
    ROLLBACK_PROMPT = (
        "ROLLBACK ALL — safe reset\n"
        "\n"
        "This will:\n"
        "  - close any running Speedy / bridge processes,\n"
        "  - delete every temp sandbox folder this tool created,\n"
        "  - delete every saved camouflage state + its history.\n"
        "\n"
        "This will KEEP the pristine baseline (the untouched copy of the\n"
        "original Speedy files), so the next boot can re-roll a DLL name\n"
        "instantly. Your real OpenSpeedy install is NOT touched.\n"
        "\n"
        "Proceed with Rollback All?"
    )

    UNPATCH_PROMPT = (
        "UNPATCH — full removal (zero footprint)\n"
        "\n"
        "This will:\n"
        "  - close any running Speedy / bridge processes,\n"
        "  - delete every temp sandbox folder this tool created,\n"
        "  - delete the ENTIRE backups folder, INCLUDING the pristine\n"
        "    baseline copy of the original Speedy files.\n"
        "\n"
        "Nothing this tool created will remain. The next boot must re-copy\n"
        "a fresh pristine baseline from your live OpenSpeedy install first,\n"
        "so that first boot is slightly slower. Your real OpenSpeedy install\n"
        "is NOT touched.\n"
        "\n"
        "Proceed with full Unpatch?"
    )

    def __init__(self, splash=None) -> None:
        self._splash = splash or _NullSplash()

        self._splash.step("Loading configuration")
        self.config = ConfigHolder()
        self.state = WatchState()
        self.state.refresher_status = "Active" if self.config.refresher_enabled else "Disabled"
        self.stop_event = threading.Event()

        self._splash.step("Sweeping orphaned temp folders")
        swept_count = stealth_temp.sweep_orphans()
        log(f"[main] Startup orphan sweep removed {swept_count} stale temp folder(s).")

        self._splash.step("Preparing Speedy store and sandbox")
        self.speedy = SpeedyDLLManager()

        self._splash.step("Wiring boot and refresher")
        self.booter = GameBooter(
            config_provider=lambda: self.config,
            state=self.state,
            stop_event=self.stop_event,
            speedy_manager=self.speedy,
        )
        self.refresher = Refresher(
            config_provider=lambda: self.config,
            state=self.state,
            stop_event=self.stop_event,
            booter=self.booter,
            reload_config=self.config.reload,
        )

        self._splash.step("Building tray icon")
        self.icon = pystray.Icon(
            TRAY_ICON_ID,
            build_icon_image(),
            TRAY_ICON_TOOLTIP,
            menu=self._build_menu(),
        )

    def _set_state(self, **fields) -> None:
        self.state.set(**fields)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _snapshot(self) -> dict:
        return self.state.snapshot()

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda i: f"Current Task: {self._snapshot()['current_task']}", None, enabled=False),
            pystray.MenuItem(lambda i: f"Is TBH on?: {self._snapshot()['tbh_state']}", None, enabled=False),
            pystray.MenuItem(lambda i: f"Game status: {self._snapshot()['game_state']}", None, enabled=False),
            pystray.MenuItem(lambda i: f"Refresher: {self._snapshot()['refresher_status']}", None, enabled=False),
            pystray.MenuItem(lambda i: f"Speedy DLL: {self._snapshot()['dll_status']}", None, enabled=False),
            pystray.MenuItem(lambda i: f"Last event: {self._snapshot()['last_event']}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Enable Refresher", self._toggle_refresher,
                             checked=lambda i: self.config.refresher_enabled),
            pystray.MenuItem("Speedy Camouflage", self._build_speedy_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Launch game now", self._launch_now),
            pystray.MenuItem("Debug", self._build_debug_menu()),
            pystray.MenuItem("Exit", self._exit),
        )

    def _build_debug_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Emergency: Clear Leftover Windows", self._emergency_clear),
            pystray.MenuItem("Run Diagnostics", self._run_diagnostics),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Config File", self._open_config),
            pystray.MenuItem("Open Log File", self._open_log),
            pystray.MenuItem("Clear Log File", self._clear_log),
        )

    def _build_speedy_menu(self) -> pystray.Menu:
        dll_name_items = [
            pystray.MenuItem(
                pair[0],
                self._make_lock_dll_handler(pair[0], pair[1]),
                checked=lambda i, name=pair[0]: self.speedy.active_64 == name,
            )
            for pair in CAMOUFLAGE_NAMES
        ]
        return pystray.Menu(
            pystray.MenuItem(lambda i: f"Active DLL: {self.speedy.active_64}", None, enabled=False),
            pystray.MenuItem("Enable DLL Randomiser", self._toggle_reroller,
                             checked=lambda i: self.config.dll_reroller_enabled),
            pystray.MenuItem("Choose DLL Name", pystray.Menu(*dll_name_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Re-roll Now", self._reroll_now),
            pystray.MenuItem("Rollback All", self._rollback_all),
            pystray.MenuItem("Unpatch (remove backups)", self._unpatch),
        )

    def _toggle_refresher(self, icon, item):
        new_value = not self.config.refresher_enabled
        update_config_key("refresher_enabled", new_value)
        self.config.reload(force=True)
        log(f"[main] Refresher toggled to {new_value}.")
        self._set_state(
            refresher_status="Waiting for game" if new_value else "Disabled",
            last_event=f"Refresher {'enabled' if new_value else 'disabled'}",
        )

    def _toggle_reroller(self, icon, item):
        new_value = not self.config.dll_reroller_enabled
        update_config_key("dll_reroller_enabled", new_value)
        self.config.reload(force=True)
        log(f"[main] DLL randomiser toggled to {new_value}.")
        self._set_state(last_event=f"DLL randomiser {'enabled' if new_value else 'disabled'}")

    def _make_lock_dll_handler(self, dll_64: str, dll_32: str):
        def handler(icon, item):
            log(f"[main] User locked camouflage to {dll_64}.")
            update_config_key("dll_reroller_enabled", False)
            self.config.reload(force=True)
            self._set_state(dll_status=f"Patching ({dll_64})...",
                            last_event=f"Locking DLL to {dll_64}")
            threading.Thread(target=self._build_camouflage, args=((dll_64, dll_32),), daemon=True).start()
        return handler

    def _build_camouflage(self, forced_names) -> None:
        build = self.speedy.prepare(force_names=forced_names)
        if build:
            self._set_state(dll_status=f"Ready ({build.dll_64})",
                            last_event=f"Camouflage built: {build.dll_64}")
        else:
            self._set_state(dll_status="Patch failed",
                            last_event="Camouflage build failed (see log)")

    def _reroll_now(self, icon, item):
        self._set_state(dll_status="Re-rolling...", last_event="Manual re-roll requested")
        threading.Thread(target=self._build_camouflage, args=(None,), daemon=True).start()

    def _rollback_all(self, icon, item):
        if not confirm_dialog("Confirm Rollback All", self.ROLLBACK_PROMPT):
            log("[main] Rollback All cancelled by user.")
            self._set_state(last_event="Rollback cancelled")
            return
        self._set_state(dll_status="Rolling back...", last_event="Rollback requested")
        threading.Thread(target=self._do_rollback, daemon=True).start()

    def _do_rollback(self):
        self.booter.close_speedy_and_helpers(timeout=QUICK_CLOSE_TIMEOUT_SECONDS)
        self.speedy.rollback_all()
        self._set_state(dll_status="Idle", last_event="Rollback complete")

    def _unpatch(self, icon, item):
        if not confirm_dialog("Confirm Unpatch", self.UNPATCH_PROMPT):
            log("[main] Unpatch cancelled by user.")
            self._set_state(last_event="Unpatch cancelled")
            return
        self._set_state(dll_status="Unpatching...", last_event="Unpatch requested")
        threading.Thread(target=self._do_unpatch, daemon=True).start()

    def _do_unpatch(self):
        self.booter.close_speedy_and_helpers(timeout=QUICK_CLOSE_TIMEOUT_SECONDS)
        self.speedy.unpatch()
        self._set_state(dll_status="Unpatched", last_event="Unpatch complete")

    def _launch_now(self, icon, item):
        log("[main] Tray action: 'Launch game now' clicked.")
        threading.Thread(target=self.booter.launch_game_flow, daemon=True).start()

    def _run_diagnostics(self, icon, item):
        log("[main] Tray action: 'Run Diagnostics' clicked.")
        threading.Thread(target=self.run_diagnostics, daemon=True).start()

    def _emergency_clear(self, icon, item):
        log("[main] Tray action: 'Emergency: Clear Leftover Windows' clicked.")
        self._set_state(last_event="Emergency window clear running...")
        threading.Thread(target=self._do_emergency_clear, daemon=True).start()

    def _do_emergency_clear(self):
        summary = self.booter.emergency_window_clear()
        self._set_state(
            last_event=(f"Emergency clear: {summary['dir_windows_closed']} win, "
                        f"{summary['procs_killed']} proc, "
                        f"{summary['blank_windows_closed']} blank"))

    def _open_config(self, icon, item):
        try:
            if not CONFIG_FILE.exists():
                save_config(DEFAULT_CONFIG)
            os.startfile(str(CONFIG_FILE))  # type: ignore[attr-defined]
        except Exception as error:
            log(f"[main] Failed to open config: {error}")

    def _open_log(self, icon, item):
        try:
            if LOG_FILE.exists():
                os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
            else:
                log("[main] Open log: file does not exist yet.")
        except Exception as error:
            log(f"[main] Failed to open log: {error}")

    def _clear_log(self, icon, item):
        try:
            open(LOG_FILE, "w", encoding="utf-8").close()
            log("[main] Log cleared via tray.")
            self._set_state(last_event="Log file cleared")
        except Exception as error:
            log(f"[main] Failed to clear log: {error}")

    def _exit(self, icon, item):
        self._set_state(current_task="Exiting", running=False, last_event="Shutting down")
        self.stop_event.set()
        try:
            self.speedy.cleanup_session()
        except Exception:
            pass
        try:
            stealth_temp.cleanup_session()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception as error:
            log(f"[main] Tray stop failed: {error}")

    def run_diagnostics(self) -> None:
        import watcher_config
        import splash as splash_module

        log("==================== TBH WATCHER DIAGNOSTICS ====================")
        self._set_state(last_event="Diagnostics: running...")

        def report(component, status, detail):
            log(f"[DIAG] [{status:4}] {component:26} {detail}")

        all_problems: list[str] = []
        all_problems += watcher_config.diagnose(report)
        all_problems += stealth_temp.diagnose(report)
        all_problems += dll_mutate.diagnose(report)
        all_problems += splash_module.diagnose(report)
        all_problems += self.speedy.diagnose(report)
        all_problems += self.booter.diagnose(report)
        all_problems += self.refresher.diagnose(report)

        log("================================================================")
        if all_problems:
            log(f"[DIAG] Completed with {len(all_problems)} issue(s): {all_problems}")
            self._set_state(last_event=f"Diagnostics: {len(all_problems)} issue(s) (see log)")
        else:
            log("[DIAG] Completed: all systems healthy.")
            self._set_state(last_event="Diagnostics: all OK")

    def monitor_loop(self) -> None:
        self._set_state(current_task="Monitoring TBH", tbh_state="No",
                        game_state="Unknown", last_event="Watcher started")
        while not self.stop_event.is_set():
            try:
                self.config.reload()

                if self.booter.busy:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if not self.booter.is_game_running():
                    self._set_state(tbh_state="No", game_state="Closed", current_task="Monitoring TBH")
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if self.booter.is_game_hung():
                    time.sleep(self.config.hang_confirm_seconds)
                    if self.booter.busy:
                        pass
                    elif self.booter.is_game_hung():
                        self.booter.handle_hang()
                    else:
                        self._set_state(tbh_state="Yes", game_state="Open",
                                        current_task="Monitoring TBH",
                                        last_event="Temporary stall cleared")
                else:
                    self._set_state(tbh_state="Yes", game_state="Open", current_task="Monitoring TBH")
            except Exception as error:
                log_error("Monitor loop error", exc=error, where="main.monitor_loop")
                self._set_state(last_event="Error in monitor loop")
            time.sleep(self.config.poll_interval_seconds)

    def run(self) -> None:
        log("[main] TBH Watcher started.")
        threading.Thread(target=self.monitor_loop, daemon=True).start()
        threading.Thread(target=self.refresher.run, daemon=True).start()

        def show_ready_notification():
            try:
                self._splash.notify_ready(self.icon)
            except Exception:
                pass
        threading.Timer(READY_NOTIFICATION_DELAY_SECONDS, show_ready_notification).start()
        self.icon.run()


def main() -> None:
    install_global_excepthook()
    log("[main] === Boot start ===")

    if not acquire_single_instance():
        log("[main] Another instance is already running; exiting this one.")
        warn_already_running()
        return

    splash = _NullSplash()
    try:
        if load_config().get("splash_enabled", True):
            splash = Splash(total_phases=STARTUP_PHASE_COUNT)
            splash.start()
        else:
            log("[main] Splash disabled via config; logging phases only.")
    except Exception as error:
        log_error("Splash failed to start (continuing without it)", exc=error, where="main")
        splash = _NullSplash()

    app = None
    try:
        app = TBHWatcher(splash=splash)
        try:
            splash.step("Starting watcher")
        except Exception:
            pass
    except Exception as error:
        log_error("FATAL: watcher failed to start", exc=error, where="main.startup")
        try:
            splash.finish()
        except Exception:
            pass
        raise
    finally:
        try:
            splash.finish()
        except Exception:
            pass

    log("[main] === Boot complete; entering tray loop ===")
    try:
        app.run()
    except Exception as error:
        log_error("FATAL: tray loop crashed", exc=error, where="main.run")
        raise


if __name__ == "__main__":
    main()
