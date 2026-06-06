from __future__ import annotations

import os
import sys
import threading
import time

import pystray

from watcher_config import (
    configFile,
    defaultConfig,
    logFile,
    WatchState,
    buildIconImage,
    confirmDialog,
    acquireSingleInstance,
    installGlobalExceptHook,
    loadConfig,
    warnAlreadyRunning,
    logInfo,
    logError,
    saveConfig,
    updateConfigKey,
)
import stealth_temp
import dll_mutate
from splash import Splash
from speedy_dll import camouflageNames, SpeedyDLLManager
from boot import GameBooter
from desktop_exile import DesktopExile
from popup_suppressor import PopupSuppressor
from refresher import Refresher

if sys.platform.startswith("win"):
    try:
        import pystray._win32
    except ImportError:
        pass


class NullSplash:
    def start(self) -> None:
        pass

    def step(self, label: str) -> None:
        pass

    def message(self, label: str) -> None:
        pass

    def notifyReady(self, icon=None) -> None:
        pass

    def finish(self) -> None:
        pass


class ConfigHolder:
    def __init__(self) -> None:
        self.lastModified = 0.0
        self.raw: dict = {}
        self.lock = threading.Lock()
        for key, value in defaultConfig.items():
            setattr(self, key, value)
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        with self.lock:
            try:
                modified = configFile.stat().st_mtime
            except OSError:
                if not configFile.exists():
                    saveConfig(defaultConfig)
                modified = 1.0
            if not force and modified == self.lastModified:
                return
            self.lastModified = modified
            self.raw = loadConfig()
            for key, value in self.raw.items():
                setattr(self, key, value)
            self.refresher_interval_seconds = self.raw.get("refresher_interval_minutes", 7.0) * 60
        logInfo(f"[main] Config (re)loaded: interval={self.raw.get('refresher_interval_minutes')}m, "
                f"cooldown={self.raw.get('refresher_cooldown_seconds')}s.")


rollbackPrompt = (
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

unpatchPrompt = (
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


class TBHWatcher:
    def __init__(self, splash=None) -> None:
        self.splash = splash or NullSplash()

        self.splash.step("Loading configuration")
        self.config = ConfigHolder()
        self.state = WatchState()
        self.state.refresherStatus = "Active" if self.config.refresher_enabled else "Disabled"
        self.stopEvent = threading.Event()

        self.splash.step("Sweeping orphaned temp folders")
        swept = stealth_temp.sweepOrphans()
        logInfo(f"[main] Startup orphan sweep removed {swept} stale temp folder(s).")

        self.splash.step("Preparing Speedy store and sandbox")
        self.speedy = SpeedyDLLManager()

        self.splash.step("Wiring boot and refresher")
        self.desktopExile = DesktopExile(
            configProvider=lambda: self.config,
            stopEvent=self.stopEvent,
            ownPid=os.getpid(),
        )
        self.popupSuppressor = PopupSuppressor(
            configProvider=lambda: self.config,
            ownPid=os.getpid(),
        )
        self.booter = GameBooter(
            configProvider=lambda: self.config,
            state=self.state,
            stopEvent=self.stopEvent,
            speedyManager=self.speedy,
            desktopExile=self.desktopExile,
            popupSuppressor=self.popupSuppressor,
        )
        self.refresher = Refresher(
            configProvider=lambda: self.config,
            state=self.state,
            stopEvent=self.stopEvent,
            booter=self.booter,
            reloadConfig=self.config.reload,
        )

        self.splash.step("Building tray icon")
        self.icon = pystray.Icon(
            "TBHWatcher",
            buildIconImage(),
            "TBH Watcher",
            menu=self.buildMenu(),
        )

    def setState(self, **fields) -> None:
        self.state.setFields(**fields)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def snapshot(self) -> dict:
        return self.state.snapshot()

    def buildMenu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Current Task: {self.snapshot()['currentTask']}", None, enabled=False),
            pystray.MenuItem(lambda item: f"Is TBH on?: {self.snapshot()['tbhState']}", None, enabled=False),
            pystray.MenuItem(lambda item: f"Game status: {self.snapshot()['gameState']}", None, enabled=False),
            pystray.MenuItem(lambda item: f"Refresher: {self.snapshot()['refresherStatus']}", None, enabled=False),
            pystray.MenuItem(lambda item: f"Speedy DLL: {self.snapshot()['dllStatus']}", None, enabled=False),
            pystray.MenuItem(lambda item: f"Last event: {self.snapshot()['lastEvent']}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Enable Refresher", self.toggleRefresher,
                             checked=lambda item: self.config.refresher_enabled),
            pystray.MenuItem("Speedy Camouflage", self.buildSpeedyMenu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Launch game now", self.launchNow),
            pystray.MenuItem("Debug", self.buildDebugMenu()),
            pystray.MenuItem("Exit", self.exit),
        )

    def buildDebugMenu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Emergency: Clear Leftover Windows", self.emergencyClear),
            pystray.MenuItem("Debug: Refresh in 10s (current cycle)", self.forceRefreshSoon),
            pystray.MenuItem("Run Diagnostics", self.runDiagnosticsAction),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Config File", self.openConfig),
            pystray.MenuItem("Open Log File", self.openLog),
            pystray.MenuItem("Clear Log File", self.clearLog),
        )

    def buildSpeedyMenu(self) -> pystray.Menu:
        dllItems = [
            pystray.MenuItem(
                pair[0],
                self.makeChooseDll(pair[0], pair[1]),
                checked=lambda item, name=pair[0]: self.speedy.active64 == name,
            )
            for pair in camouflageNames
        ]
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Active DLL: {self.speedy.active64}", None, enabled=False),
            pystray.MenuItem("Enable DLL Randomiser", self.toggleReroller,
                             checked=lambda item: self.config.dll_reroller_enabled),
            pystray.MenuItem("Choose DLL Name", pystray.Menu(*dllItems)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Re-roll Now", self.rerollNow),
            pystray.MenuItem("Rollback All", self.rollbackAll),
            pystray.MenuItem("Unpatch (remove backups)", self.unpatch),
        )

    def toggleRefresher(self, icon, item):
        newValue = not self.config.refresher_enabled
        updateConfigKey("refresher_enabled", newValue)
        self.config.reload(force=True)
        logInfo(f"[main] Refresher toggled to {newValue}.")
        self.setState(
            refresherStatus="Waiting for game" if newValue else "Disabled",
            lastEvent=f"Refresher {'enabled' if newValue else 'disabled'}",
        )

    def toggleReroller(self, icon, item):
        newValue = not self.config.dll_reroller_enabled
        updateConfigKey("dll_reroller_enabled", newValue)
        self.config.reload(force=True)
        logInfo(f"[main] DLL randomiser toggled to {newValue}.")
        self.setState(lastEvent=f"DLL randomiser {'enabled' if newValue else 'disabled'}")

    def makeChooseDll(self, dll64: str, dll32: str):
        def handler(icon, item):
            logInfo(f"[main] User locked camouflage to {dll64}.")
            updateConfigKey("dll_reroller_enabled", False)
            self.config.reload(force=True)
            self.setState(dllStatus=f"Patching ({dll64})...", lastEvent=f"Locking DLL to {dll64}")
            threading.Thread(target=self.patchTo, args=((dll64, dll32),), daemon=True).start()
        return handler

    def patchTo(self, names) -> None:
        build = self.speedy.prepare(forceNames=names)
        if build:
            self.setState(dllStatus=f"Ready ({build.dll64})", lastEvent=f"Camouflage built: {build.dll64}")
        else:
            self.setState(dllStatus="Patch failed", lastEvent="Camouflage build failed (see log)")

    def rerollNow(self, icon, item):
        self.setState(dllStatus="Re-rolling...", lastEvent="Manual re-roll requested")
        threading.Thread(target=self.patchTo, args=(None,), daemon=True).start()

    def rollbackAll(self, icon, item):
        if not confirmDialog("Confirm Rollback All", rollbackPrompt):
            logInfo("[main] Rollback All cancelled by user.")
            self.setState(lastEvent="Rollback cancelled")
            return
        self.setState(dllStatus="Rolling back...", lastEvent="Rollback requested")
        threading.Thread(target=self.doRollback, daemon=True).start()

    def doRollback(self):
        self.booter.closeSpeedyAndHelpers(timeout=2)
        self.speedy.rollbackAll()
        self.setState(dllStatus="Idle", lastEvent="Rollback complete")

    def unpatch(self, icon, item):
        if not confirmDialog("Confirm Unpatch", unpatchPrompt):
            logInfo("[main] Unpatch cancelled by user.")
            self.setState(lastEvent="Unpatch cancelled")
            return
        self.setState(dllStatus="Unpatching...", lastEvent="Unpatch requested")
        threading.Thread(target=self.doUnpatch, daemon=True).start()

    def doUnpatch(self):
        self.booter.closeSpeedyAndHelpers(timeout=2)
        self.speedy.unpatch()
        self.setState(dllStatus="Unpatched", lastEvent="Unpatch complete")

    def launchNow(self, icon, item):
        logInfo("[main] Tray action: 'Launch game now' clicked.")
        threading.Thread(target=self.booter.launchGameFlow, daemon=True).start()

    def runDiagnosticsAction(self, icon, item):
        logInfo("[main] Tray action: 'Run Diagnostics' clicked.")
        threading.Thread(target=self.runDiagnostics, daemon=True).start()

    def forceRefreshSoon(self, icon, item):
        logInfo("[main] Tray action: 'Debug: Refresh in 10s' clicked.")
        try:
            ok, message = self.refresher.forceRefreshSoon(10.0)
        except Exception as error:
            ok, message = False, str(error)
        self.setState(lastEvent=f"Debug refresh-in-10s: {message}")
        logInfo(f"[main] Debug refresh-in-10s -> {'OK' if ok else 'declined'}: {message}")

    def emergencyClear(self, icon, item):
        logInfo("[main] Tray action: 'Emergency: Clear Leftover Windows' clicked.")
        self.setState(lastEvent="Emergency window clear running...")
        threading.Thread(target=self.doEmergencyClear, daemon=True).start()

    def doEmergencyClear(self):
        summary = self.booter.emergencyWindowClear()
        self.setState(lastEvent=(f"Emergency clear: {summary['dir_windows_closed']} win, "
                                 f"{summary['procs_killed']} proc, "
                                 f"{summary['blank_windows_closed']} blank"))

    def openConfig(self, icon, item):
        try:
            if not configFile.exists():
                saveConfig(defaultConfig)
            os.startfile(str(configFile))
        except Exception as error:
            logInfo(f"[main] Failed to open config: {error}")

    def openLog(self, icon, item):
        try:
            if logFile.exists():
                os.startfile(str(logFile))
            else:
                logInfo("[main] Open log: file does not exist yet.")
        except Exception as error:
            logInfo(f"[main] Failed to open log: {error}")

    def clearLog(self, icon, item):
        try:
            open(logFile, "w", encoding="utf-8").close()
            logInfo("[main] Log cleared via tray.")
            self.setState(lastEvent="Log file cleared")
        except Exception as error:
            logInfo(f"[main] Failed to clear log: {error}")

    def exit(self, icon, item):
        self.setState(currentTask="Exiting", running=False, lastEvent="Shutting down")
        self.stopEvent.set()
        try:
            self.popupSuppressor.stop()
        except Exception:
            pass
        try:
            self.desktopExile.cleanup()
        except Exception:
            pass
        try:
            self.speedy.cleanupSession()
        except Exception:
            pass
        try:
            stealth_temp.cleanupSession()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception as error:
            logInfo(f"[main] Tray stop failed: {error}")

    def runDiagnostics(self) -> None:
        import watcher_config

        logInfo("==================== TBH WATCHER DIAGNOSTICS ====================")
        self.setState(lastEvent="Diagnostics: running...")

        def report(component, status, detail):
            logInfo(f"[DIAG] [{status:4}] {component:26} {detail}")

        import splash as splashModule
        allIssues: list[str] = []
        allIssues += watcher_config.diagnose(report)
        allIssues += stealth_temp.diagnose(report)
        allIssues += dll_mutate.diagnose(report)
        allIssues += splashModule.diagnose(report)
        allIssues += self.speedy.diagnose(report)
        allIssues += self.booter.diagnose(report)
        allIssues += self.refresher.diagnose(report)

        logInfo("================================================================")
        if allIssues:
            logInfo(f"[DIAG] Completed with {len(allIssues)} issue(s): {allIssues}")
            self.setState(lastEvent=f"Diagnostics: {len(allIssues)} issue(s) (see log)")
        else:
            logInfo("[DIAG] Completed: all systems healthy.")
            self.setState(lastEvent="Diagnostics: all OK")

    def monitorLoop(self) -> None:
        self.setState(currentTask="Monitoring TBH", tbhState="No",
                      gameState="Unknown", lastEvent="Watcher started")
        while not self.stopEvent.is_set():
            try:
                self.config.reload()

                if self.booter.busy:
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if not self.booter.isGameRunning():
                    self.setState(tbhState="No", gameState="Closed", currentTask="Monitoring TBH")
                    time.sleep(self.config.poll_interval_seconds)
                    continue

                if self.booter.isGameHung():
                    time.sleep(self.config.hang_confirm_seconds)
                    if self.booter.busy:
                        pass
                    elif self.booter.isGameHung():
                        self.booter.handleHang()
                    else:
                        self.setState(tbhState="Yes", gameState="Open",
                                      currentTask="Monitoring TBH",
                                      lastEvent="Temporary stall cleared")
                else:
                    self.setState(tbhState="Yes", gameState="Open", currentTask="Monitoring TBH")
            except Exception as error:
                logError("Monitor loop error", error=error, where="main.monitorLoop")
                self.setState(lastEvent="Error in monitor loop")
            time.sleep(self.config.poll_interval_seconds)

    def run(self) -> None:
        logInfo("[main] TBH Watcher started.")
        threading.Thread(target=self.monitorLoop, daemon=True).start()
        threading.Thread(target=self.refresher.run, daemon=True).start()

        def announceReady():
            try:
                self.splash.notifyReady(self.icon)
            except Exception:
                pass
        timer = threading.Timer(1.5, announceReady)
        timer.daemon = True
        timer.start()
        self.icon.run()


def main() -> None:
    installGlobalExceptHook()
    logInfo("[main] === Boot start ===")

    if not acquireSingleInstance():
        logInfo("[main] Another instance is already running; exiting this one.")
        warnAlreadyRunning()
        return

    splash = NullSplash()
    try:
        if loadConfig().get("splash_enabled", True):
            splash = Splash(totalSteps=6)
            splash.start()
        else:
            logInfo("[main] Splash disabled via config; logging phases only.")
    except Exception as error:
        logError("Splash failed to start (continuing without it)", error=error, where="main")
        splash = NullSplash()

    application = None
    try:
        application = TBHWatcher(splash=splash)
        try:
            splash.step("Starting watcher")
        except Exception:
            pass
    except Exception as error:
        logError("FATAL: watcher failed to start", error=error, where="main.startup")
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

    logInfo("[main] === Boot complete; entering tray loop ===")
    try:
        application.run()
    except Exception as error:
        logError("FATAL: tray loop crashed", error=error, where="main.run")
        raise


if __name__ == "__main__":
    main()
