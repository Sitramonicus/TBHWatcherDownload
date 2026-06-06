from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import psutil

from watcher_config import (
    logInfo,
    logError,
    getWindowPid,
    dataDir,
    user32,
    minimiseWindow,
    hideWindow,
    closeWindow,
    forceForegroundOnce,
    stopFlashing,
    getForegroundWindow,
)

backgroundDesktopName = "TBHWatcher-bg"
metaFileName = "TBHWatcher_desktop.json"


def pyvdaAvailable() -> bool:
    try:
        import pyvda
        return True
    except Exception:
        return False


class DesktopExile:
    def __init__(self, configProvider: Callable, stopEvent: threading.Event,
                 ownPid: Optional[int] = None):
        self.configProvider = configProvider
        self.stopEvent = stopEvent
        self.ownPid = ownPid if ownPid is not None else os.getpid()
        self.lock = threading.RLock()
        self.createdDesktop = False
        self.backgroundId = ""
        self.metaPath = Path(dataDir) / metaFileName
        self.injectionConfirmed = threading.Event()
        self.exiledHandles = set()
        self.gameHandledOnce = False
        self.gameExiledApps = {}
        self.gameBroughtBack = False
        self.previousForeground = 0
        self.focusRestored = False
        self.removedFocusStealer = False

    def config(self, key, default=None):
        return getattr(self.configProvider(), key, default)

    def desktopWorkRequested(self) -> bool:
        return (bool(self.config("desktop_exile_enabled", True))
                or bool(self.config("desktop_exile_game_to_hidden", True)))

    def steamSweepRequested(self) -> bool:
        return bool(self.config("desktop_exile_enabled", True))

    def enabled(self) -> bool:
        if not self.desktopWorkRequested():
            return False
        if not pyvdaAvailable():
            logInfo("[desktop] pyvda not available; desktop work disabled (graceful fallback).")
            return False
        return True

    def readMeta(self) -> dict:
        try:
            if self.metaPath.exists():
                with open(self.metaPath, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def writeMeta(self, data: dict) -> None:
        try:
            self.metaPath.parent.mkdir(parents=True, exist_ok=True)
            temporaryPath = self.metaPath.with_suffix(".json.tmp")
            with open(temporaryPath, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporaryPath, self.metaPath)
        except Exception as error:
            logInfo(f"[desktop] Could not persist desktop meta: {error}")

    def clearMeta(self) -> None:
        try:
            if self.metaPath.exists():
                self.metaPath.unlink()
        except Exception:
            pass

    def listDesktops(self):
        from pyvda import get_virtual_desktops
        return get_virtual_desktops()

    def currentDesktop(self):
        from pyvda import VirtualDesktop
        return VirtualDesktop.current()

    def desktopGuid(self, desktop) -> str:
        try:
            return str(desktop.id)
        except Exception:
            return ""

    def desktopName(self, desktop) -> str:
        try:
            return desktop.name or ""
        except Exception:
            return ""

    def findById(self, guid: str):
        if not guid:
            return None
        try:
            for desktop in self.listDesktops():
                if self.desktopGuid(desktop) == guid:
                    return desktop
        except Exception:
            return None
        return None

    def findByName(self):
        try:
            for desktop in self.listDesktops():
                if self.desktopName(desktop) == backgroundDesktopName:
                    return desktop
        except Exception:
            return None
        return None

    def ensureBackgroundDesktop(self):
        from pyvda import VirtualDesktop
        savedId = self.readMeta().get("background_desktop_id", "")
        existing = self.findById(savedId) or self.findByName()
        if existing is not None:
            self.backgroundId = self.desktopGuid(existing)
            logInfo(f"[desktop] Reusing background desktop (number {existing.number}, "
                    f"name '{self.desktopName(existing)}').")
            return existing

        created = VirtualDesktop.create()
        try:
            created.rename(backgroundDesktopName)
        except Exception as error:
            logInfo(f"[desktop] Background desktop rename unsupported on this build "
                    f"({error}); tracking by id instead.")
        self.createdDesktop = True
        self.backgroundId = self.desktopGuid(created)
        self.writeMeta({"background_desktop_id": self.backgroundId, "pid": self.ownPid})
        logInfo(f"[desktop] Created background desktop (number {created.number}).")
        return created

    def isOurWindow(self, hwnd: int) -> bool:
        try:
            return getWindowPid(hwnd) == self.ownPid
        except Exception:
            return False

    def windowIsVisible(self, hwnd: int) -> bool:
        if user32 is None or not hwnd:
            return False
        try:
            return bool(user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    def windowIsMinimised(self, hwnd: int) -> bool:
        if user32 is None or not hwnd:
            return False
        try:
            return bool(user32.IsIconic(hwnd))
        except Exception:
            return False

    def windowProcessName(self, hwnd: int) -> str:
        try:
            pid = getWindowPid(hwnd)
            if not pid:
                return ""
            return psutil.Process(pid).name().lower()
        except Exception:
            return ""

    def isSteamWindow(self, processName: str) -> bool:
        return (processName or "").lower() == "steam.exe"

    def isGameWindow(self, title: str, processName: str) -> bool:
        gameProcess = self.config("task_process_name", "TaskBarHero.exe").lower()
        loweredProcess = (processName or "").lower()
        return bool(gameProcess) and loweredProcess == gameProcess

    def windowTitle(self, hwnd: int) -> str:
        if user32 is None:
            return ""
        import ctypes
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:
            return ""

    def iterCandidateWindows(self):
        from pyvda import get_apps_by_z_order
        seen = set()
        try:
            for app in get_apps_by_z_order(current_desktop=False):
                try:
                    hwnd = app.hwnd
                except Exception:
                    continue
                if hwnd and hwnd not in seen:
                    seen.add(hwnd)
                    yield hwnd, app
        except Exception as error:
            logInfo(f"[desktop] get_apps_by_z_order failed: {error}")

    def appOnDesktop(self, app, desktop) -> bool:
        try:
            return app.desktop.number == desktop.number
        except Exception:
            return False

    def moveApp(self, app, hwnd: int, desktop) -> bool:
        try:
            if self.appOnDesktop(app, desktop):
                return False
            app.move(desktop)
            return True
        except Exception as error:
            logInfo(f"[desktop] Could not move hwnd {hwnd}: {error}")
            return False

    def signalInjectionConfirmed(self) -> None:
        self.injectionConfirmed.set()

    def restoreUserFocus(self) -> None:
        if self.focusRestored:
            return
        if not self.config("desktop_exile_restore_focus", True):
            self.focusRestored = True
            return
        if not self.previousForeground:
            self.focusRestored = True
            return
        try:
            current = getForegroundWindow()
        except Exception:
            current = 0
        if current == self.previousForeground:
            self.focusRestored = True
            return
        if not self.windowExists(self.previousForeground):
            self.focusRestored = True
            return
        self.focusRestored = True
        restored = forceForegroundOnce(self.previousForeground)
        stopFlashing(self.previousForeground)
        logInfo(f"[desktop] Focus restore to pre-launch window "
                f"({'ok' if restored else 'attempted'}; one-shot, no maximize/loop).")

    def reset(self) -> None:
        self.injectionConfirmed.clear()
        self.exiledHandles = set()
        self.gameHandledOnce = False
        self.gameExiledApps = {}
        self.gameBroughtBack = False
        self.previousForeground = 0
        self.focusRestored = False
        self.removedFocusStealer = False

    def rememberForeground(self) -> None:
        try:
            handle = getForegroundWindow()
        except Exception:
            handle = 0
        if handle and (self.isOurWindow(handle) or self.isSteamWindow(self.windowProcessName(handle))
                       or self.isGameWindow("", self.windowProcessName(handle))):
            handle = 0
        self.previousForeground = handle

    def steamRunning(self) -> bool:
        try:
            for process in psutil.process_iter(["name"]):
                if (process.info.get("name") or "").lower() == "steam.exe":
                    return True
        except Exception:
            pass
        return False

    def waitForSteamWindow(self, background, timeoutSeconds: float) -> int:
        deadline = time.time() + timeoutSeconds
        moved = 0
        while time.time() < deadline:
            if self.stopEvent.is_set():
                return moved
            moved += self.sweepSteam(background)
            if moved:
                return moved
            time.sleep(0.3)
        return moved

    def sweepSteam(self, background) -> int:
        movedCount = 0
        for hwnd, app in self.iterCandidateWindows():
            if self.isOurWindow(hwnd):
                continue
            processName = self.windowProcessName(hwnd)
            if not self.isSteamWindow(processName):
                continue
            title = self.windowTitle(hwnd)
            if self.config("desktop_exile_hide_popup", True):
                hideWindow(hwnd)
            if self.moveApp(app, hwnd, background):
                movedCount += 1
            self.exiledHandles.add(hwnd)
            self.removedFocusStealer = True
            logInfo(f"[desktop] Exiled Steam window '{title}' "
                    f"(hidden+moved to desktop {background.number}).")
        return movedCount

    def gameToHidden(self) -> bool:
        return bool(self.config("desktop_exile_game_to_hidden", True))

    def handleGameWindow(self, background) -> None:
        if self.injectionConfirmed.is_set():
            return
        if self.gameToHidden():
            self.exileGameToHidden(background)
            return
        if self.gameHandledOnce:
            return
        exileGame = bool(self.config("desktop_exile_game_enabled", False))
        minimiseGame = bool(self.config("desktop_exile_minimise_game", True))
        if not exileGame and not minimiseGame:
            self.gameHandledOnce = True
            return
        for hwnd, app in self.iterCandidateWindows():
            if self.isOurWindow(hwnd):
                continue
            processName = self.windowProcessName(hwnd)
            title = self.windowTitle(hwnd)
            if not self.isGameWindow(title, processName):
                continue
            if exileGame:
                if self.moveApp(app, hwnd, background):
                    logInfo(f"[desktop] Exiled game window '{title}' to desktop "
                            f"{background.number} (desktop_exile_game_enabled).")
            else:
                if not self.windowIsVisible(hwnd) or self.windowIsMinimised(hwnd):
                    return
                if minimiseWindow(hwnd):
                    logInfo(f"[desktop] Minimised game window '{title}' once at boot "
                            f"(will not touch it again).")
            self.gameHandledOnce = True
            return

    def exileGameToHidden(self, background) -> None:
        if background is None:
            return
        for hwnd, app in self.iterCandidateWindows():
            if self.isOurWindow(hwnd):
                continue
            if hwnd in self.gameExiledApps:
                continue
            processName = self.windowProcessName(hwnd)
            title = self.windowTitle(hwnd)
            if not self.isGameWindow(title, processName):
                continue
            if self.appOnDesktop(app, background):
                self.gameExiledApps[hwnd] = app
                continue
            if self.moveApp(app, hwnd, background):
                self.gameExiledApps[hwnd] = app
                self.removedFocusStealer = True
                logInfo(f"[desktop] Moved game window '{title}' to the hidden desktop "
                        f"{background.number} for boot.")

    def bringGameBack(self) -> None:
        with self.lock:
            if self.gameBroughtBack:
                return
            self.gameBroughtBack = True
        try:
            current = self.currentDesktop()
        except Exception:
            current = None
        broughtBack = 0
        for hwnd, app in list(self.gameExiledApps.items()):
            try:
                if not self.windowExists(hwnd):
                    continue
                if self.isOurWindow(hwnd):
                    continue
                if current is not None and not self.appOnDesktop(app, current):
                    self.moveApp(app, hwnd, current)
                minimiseWindow(hwnd)
                broughtBack += 1
            except Exception as error:
                logInfo(f"[desktop] Restore of game hwnd {hwnd} failed: {error}")
        if broughtBack:
            logInfo(f"[desktop] Brought {broughtBack} game window(s) back to the main "
                    f"desktop (minimised).")
        self.gameExiledApps = {}

    def prelaunchExile(self) -> Optional[object]:
        if not self.enabled():
            return None
        with self.lock:
            background = self.ensureBackgroundDesktop()
            if background is None:
                return None
            if self.steamSweepRequested() and self.config("desktop_exile_prelaunch_steam", True):
                if self.steamRunning():
                    self.sweepSteam(background)
                else:
                    logInfo("[desktop] Steam not running yet; will sweep its popup once it appears.")
            return background

    def switchLaunchEnabled(self) -> bool:
        return (self.enabled()
                and bool(self.config("desktop_exile_switch_launch", False)))

    def switchLaunch(self, background, launchCallback) -> bool:
        from pyvda import VirtualDesktop
        try:
            origin = self.currentDesktop()
        except Exception:
            origin = None
        try:
            background.go(allow_set_foreground=False)
            logInfo("[desktop] switch-launch: switched to background desktop to launch.")
        except Exception as error:
            logInfo(f"[desktop] switch-launch: could not switch to background ({error}); "
                    f"launching on current desktop instead.")
            launchCallback()
            return False
        try:
            launchCallback()
        finally:
            backDelay = max(0.0, float(self.config("desktop_exile_switch_back_seconds", 1.5)))
            time.sleep(backDelay)
            try:
                if origin is not None:
                    origin.go(allow_set_foreground=False)
                    logInfo("[desktop] switch-launch: switched back to your desktop.")
            except Exception as error:
                logInfo(f"[desktop] switch-launch: could not switch back ({error}).")
        return True

    def run(self, background=None) -> None:
        try:
            self.runImpl(background)
        except Exception as error:
            logError("Desktop exile crashed", error, where="desktop_exile.run")

    def runImpl(self, background=None) -> None:
        if not self.enabled():
            return
        with self.lock:
            if background is None:
                background = self.ensureBackgroundDesktop()
        if background is None:
            logInfo("[desktop] No background desktop; skipping exile.")
            return
        self.runWatch(background)

    def runWatch(self, background) -> None:
        watchSeconds = max(0.0, float(self.config("desktop_exile_watch_seconds", 40.0)))
        pollSeconds = max(0.2, float(self.config("desktop_exile_poll_seconds", 0.5)))
        fastSeconds = max(0.0, float(self.config("desktop_exile_fast_seconds", 8.0)))
        fastPoll = max(0.02, float(self.config("desktop_exile_fast_poll_seconds", 0.05)))

        logInfo(f"[desktop] Exile watch armed (up to {watchSeconds:.0f}s, "
                f"fast {fastSeconds:.0f}s@{fastPoll:.2f}s then poll {pollSeconds:.2f}s, "
                f"target desktop {background.number}).")
        fastUntil = time.time() + fastSeconds
        deadline = time.time() + watchSeconds
        totalMoved = 0
        confirmed = False
        while time.time() < deadline:
            if self.stopEvent.is_set():
                return
            if self.steamSweepRequested():
                totalMoved += self.sweepSteam(background)
            self.handleGameWindow(background)
            if self.removedFocusStealer:
                self.restoreUserFocus()
            if self.injectionConfirmed.is_set():
                confirmed = True
                logInfo("[desktop] Injection confirmed; bringing Steam back to main (minimised).")
                break
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            activePoll = fastPoll if time.time() < fastUntil else pollSeconds
            sleepFor = min(activePoll, remaining)
            slept = 0.0
            while slept < sleepFor:
                if self.stopEvent.is_set():
                    return
                if self.injectionConfirmed.is_set():
                    break
                chunk = min(0.05, sleepFor - slept)
                time.sleep(chunk)
                slept += chunk

        if not confirmed:
            logInfo(f"[desktop] Exile watch ended without injection confirmation "
                    f"({totalMoved} Steam window(s) exiled); bringing Steam + game back so "
                    f"they are not stranded.")
        self.bringSteamBack()
        self.bringGameBack()

    def windowExists(self, hwnd: int) -> bool:
        if user32 is None or not hwnd:
            return False
        try:
            return bool(user32.IsWindow(hwnd))
        except Exception:
            return False

    def restoreSteamWindow(self, hwnd: int, current) -> bool:
        if user32 is not None:
            try:
                user32.ShowWindow(hwnd, 9)
            except Exception:
                pass
        try:
            from pyvda import AppView
            view = AppView(hwnd=hwnd)
            if current is not None and view.desktop.number != current.number:
                view.move(current)
        except Exception as error:
            logInfo(f"[desktop] Could not move Steam hwnd {hwnd} back: {error}")
        if self.config("desktop_exile_steam_to_tray", True):
            closeWindow(hwnd)
            return True
        minimiseWindow(hwnd)
        return True

    def bringSteamBack(self) -> None:
        try:
            current = self.currentDesktop()
        except Exception:
            current = None
        broughtBack = 0
        for hwnd in list(self.exiledHandles):
            try:
                if not self.windowExists(hwnd):
                    continue
                if self.isOurWindow(hwnd):
                    continue
                if self.restoreSteamWindow(hwnd, current):
                    broughtBack += 1
            except Exception as error:
                logInfo(f"[desktop] Restore of Steam hwnd {hwnd} failed: {error}")
        toTray = self.config("desktop_exile_steam_to_tray", True)
        if broughtBack:
            logInfo(f"[desktop] Brought {broughtBack} Steam window(s) back to the main "
                    f"desktop ({'tray' if toTray else 'minimised'}).")
        self.exiledHandles = set()

    def cleanup(self) -> None:
        try:
            self.cleanupImpl()
        except Exception as error:
            logError("Desktop exile cleanup crashed", error, where="desktop_exile.cleanup")

    def cleanupImpl(self) -> None:
        if not pyvdaAvailable():
            return
        if not self.createdDesktop:
            return

        background = self.findById(self.backgroundId) or self.findByName()
        if background is None:
            self.createdDesktop = False
            self.clearMeta()
            return

        current = None
        try:
            current = self.currentDesktop()
        except Exception:
            current = None

        rescueTarget = None
        try:
            for desktop in self.listDesktops():
                if self.desktopGuid(desktop) != self.desktopGuid(background):
                    if current is not None and self.desktopGuid(desktop) == self.desktopGuid(current):
                        rescueTarget = desktop
                        break
                    if rescueTarget is None:
                        rescueTarget = desktop
        except Exception:
            rescueTarget = None

        if rescueTarget is not None:
            self.rescueStragglers(background, rescueTarget)

        try:
            if current is not None:
                background.remove(fallback=current)
            else:
                background.remove()
            logInfo("[desktop] Removed background desktop on exit.")
        except Exception as error:
            logInfo(f"[desktop] Could not remove background desktop: {error}")

        self.createdDesktop = False
        self.clearMeta()

    def rescueStragglers(self, background, rescueTarget) -> None:
        from pyvda import get_apps_by_z_order
        try:
            for app in get_apps_by_z_order(current_desktop=False):
                try:
                    hwnd = app.hwnd
                except Exception:
                    continue
                try:
                    if app.desktop.number == background.number:
                        app.move(rescueTarget)
                        logInfo(f"[desktop] Rescued straggler hwnd {hwnd} to "
                                f"desktop {rescueTarget.number} before removal.")
                except Exception:
                    continue
        except Exception as error:
            logInfo(f"[desktop] Straggler rescue pass failed: {error}")

    def diagnose(self, report) -> list:
        issues = []
        available = pyvdaAvailable()
        report("desktop.pyvda", "OK" if available else "WARN",
               "pyvda importable" if available else "pyvda not installed (feature disabled)")
        report("desktop.enabled", "OK",
               f"desktop_exile_enabled={self.config('desktop_exile_enabled', True)}")
        if available:
            try:
                count = len(self.listDesktops())
                report("desktop.enumerate", "OK", f"{count} virtual desktop(s)")
            except Exception as error:
                report("desktop.enumerate", "FAIL", str(error))
                issues.append("desktop enumerate raised")
        return issues
