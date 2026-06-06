from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path
from typing import Callable, Optional

import psutil

from watcher_config import (
    closeWindow,
    diagnoseOwnWindows,
    stripFromTaskbar,
    enumAllTopLevelWindows,
    enumWindowsForPid,
    getForegroundWindow,
    getWindowPid,
    hideWindow,
    isWindowHung,
    logInfo,
    logError,
    setForeground,
)
from speedy_dll import (
    SpeedyDLLManager,
    SpeedyBuild,
    camouflageNames,
    original64,
    original32,
)
import stealth_temp


allInjectableDllNames = {original64.lower(), original32.lower()}
for coverName64, coverName32 in camouflageNames:
    allInjectableDllNames.add(coverName64.lower())
    allInjectableDllNames.add(coverName32.lower())

swHide = 0
detachedProcess = 0x00000008
focusThiefKeywords = ("steam", "taskbarhero", "speedy", "bridge")


class GameBooter:
    def __init__(self, configProvider: Callable, state, stopEvent: threading.Event,
                 speedyManager: SpeedyDLLManager, desktopExile=None,
                 popupSuppressor=None) -> None:
        self.configProvider = configProvider
        self.state = state
        self.stopEvent = stopEvent
        self.speedy = speedyManager
        self.desktopExile = desktopExile
        self.popupSuppressor = popupSuppressor
        self.launchLock = threading.Lock()
        self.busyFlag = threading.Event()
        self.busyTimer = None

    @property
    def busy(self) -> bool:
        return self.busyFlag.is_set()

    def markBusy(self, safetyClearAfter: float = 30.0) -> None:
        self.busyFlag.set()
        if self.busyTimer is not None:
            try:
                self.busyTimer.cancel()
            except Exception:
                pass
        timer = threading.Timer(safetyClearAfter, self.busyFlag.clear)
        timer.daemon = True
        self.busyTimer = timer
        timer.start()

    def clearBusy(self) -> None:
        if self.busyTimer is not None:
            try:
                self.busyTimer.cancel()
            except Exception:
                pass
        self.busyFlag.clear()

    def config(self, key, default=None):
        return getattr(self.configProvider(), key, default)

    def processesNamed(self, name: str) -> list:
        matches: list = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if process.info["name"] == name:
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as error:
                logInfo(f"[boot] process_iter error: {error}")
        return matches

    def pidSnapshot(self) -> set:
        try:
            return {process.pid for process in psutil.process_iter(["pid"])}
        except Exception as error:
            logInfo(f"[boot] pid snapshot error: {error}")
            return set()

    def killTree(self, pid: int, timeout: int = 10) -> bool:
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except Exception as error:
            logInfo(f"[boot] killTree lookup error: {error}")
            return False

        try:
            children = root.children(recursive=True)
        except Exception:
            children = []
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            root.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        try:
            _, alive = psutil.wait_procs([root, *children], timeout=timeout)
            for process in alive:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, stillAlive = psutil.wait_procs(alive, timeout=timeout)
            return not stillAlive
        except Exception as error:
            logInfo(f"[boot] killTree wait error: {error}")
            return False

    def ensureClosed(self, name: str, timeout: int = 10) -> bool:
        closedAll = all(self.killTree(process.pid, timeout=timeout)
                        for process in self.processesNamed(name))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.processesNamed(name):
                return True
            time.sleep(0.5)
        return closedAll and not self.processesNamed(name)

    def processesUnderDir(self, directory) -> list:
        matches: list = []
        try:
            target = str(Path(directory).resolve()).lower()
        except Exception:
            return matches
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                executable = process.info.get("exe")
                if executable and str(Path(executable).resolve()).lower().startswith(target):
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        return matches

    def closeSpeedyAndHelpers(self, timeout: int = 5, build=None) -> None:
        killedCount = 0
        directories = set()
        try:
            directories.add(self.speedy.sandboxDir)
            directories.add(self.speedy.storeDir)
        except Exception:
            pass
        if build is not None:
            try:
                directories.add(build.sandboxDir)
            except Exception:
                pass
        for directory in directories:
            for process in self.processesUnderDir(directory):
                if self.killTree(process.pid, timeout=timeout):
                    killedCount += 1
        for name in (self.config("speedy_process_name", "Speedy.exe"),
                     "Speedy.exe", "bridge64.exe", "bridge32.exe"):
            if name:
                self.ensureClosed(name, timeout=timeout)
        if killedCount:
            logInfo(f"[boot] Closed {killedCount} injector process(es) running from our temp folders.")

    def hideProcessWindows(self, name: str) -> None:
        for process in self.processesNamed(name):
            try:
                for handle in enumWindowsForPid(process.pid, onlyVisible=True):
                    hideWindow(handle)
            except Exception as error:
                logInfo(f"[boot] window-hide error for {process.pid}: {error}")

    def closeWindowsInDir(self, directory) -> None:
        for process in self.processesUnderDir(directory):
            try:
                for handle in enumWindowsForPid(process.pid, onlyVisible=False):
                    closeWindow(handle)
            except Exception as error:
                logInfo(f"[boot] window-close error for {process.pid}: {error}")

    def emergencyWindowClear(self) -> dict:
        summary = {"dir_windows_closed": 0, "procs_killed": 0,
                   "blank_windows_closed": 0, "orphans_swept": 0,
                   "own_stripped": 0}
        try:
            for window in diagnoseOwnWindows(os.getpid()):
                if window["likely_taskbar"] and stripFromTaskbar(window["hwnd"]):
                    summary["own_stripped"] += 1
        except Exception as error:
            logInfo(f"[boot] emergency: own-window strip error: {error}")

        directories = set()
        try:
            directories.add(self.speedy.sandboxDir)
            directories.add(self.speedy.storeDir)
        except Exception:
            pass
        for directory in directories:
            try:
                for process in self.processesUnderDir(directory):
                    try:
                        for handle in enumWindowsForPid(process.pid, onlyVisible=False):
                            closeWindow(handle)
                            summary["dir_windows_closed"] += 1
                    except Exception:
                        pass
                    if self.killTree(process.pid, timeout=3):
                        summary["procs_killed"] += 1
            except Exception as error:
                logInfo(f"[boot] emergency: dir cleanup error: {error}")

        try:
            summary["orphans_swept"] = stealth_temp.sweepOrphans()
        except Exception as error:
            logInfo(f"[boot] emergency: orphan sweep error: {error}")

        try:
            temporaryRoot = str(Path(tempfile.gettempdir()).resolve()).lower()
            for handle, pid, title, _visible in enumAllTopLevelWindows():
                if title.strip():
                    continue
                try:
                    executable = (psutil.Process(pid).exe() or "").lower()
                except Exception:
                    executable = ""
                if executable and str(Path(executable).resolve()).lower().startswith(temporaryRoot):
                    closeWindow(handle)
                    summary["blank_windows_closed"] += 1
        except Exception as error:
            logInfo(f"[boot] emergency: blank-window scan error: {error}")
        logInfo(f"[boot] Emergency window clear done: {summary}")
        return summary

    def loadedDllPaths(self, pid: int) -> set:
        paths: set = set()
        try:
            process = psutil.Process(pid)
            for region in process.memory_maps():
                lowered = region.path.lower()
                if lowered.endswith(".dll"):
                    paths.add(lowered)
        except psutil.AccessDenied:
            logInfo("[boot] DLL detection: access denied reading memory maps (need admin).")
        except Exception as error:
            logInfo(f"[boot] DLL detection error: {error}")
        return paths

    def isAlreadyInjected(self, pid: int) -> tuple:
        try:
            loaded = self.loadedDllPaths(pid)
        except Exception as error:
            logInfo(f"[boot] inject-check: could not read modules: {error}")
            return (False, "")
        if not loaded:
            return (False, "")
        for fullPath in loaded:
            baseName = Path(fullPath).name.lower()
            if baseName in allInjectableDllNames:
                return (True, baseName)
        return (False, "")

    def isGameRunning(self) -> bool:
        return bool(self.processesNamed(self.config("task_process_name", "TaskBarHero.exe")))

    def isGameHung(self) -> bool:
        for process in self.processesNamed(self.config("task_process_name", "TaskBarHero.exe")):
            try:
                if psutil.Process(process.pid).status() == psutil.STATUS_ZOMBIE:
                    return True
                windows = enumWindowsForPid(process.pid, onlyVisible=True)
                if windows and any(isWindowHung(handle) for handle in windows):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def launchGame(self) -> None:
        appId = self.config("game_app_id", "3678970")
        uri = f"steam://rungameid/{appId}"
        logInfo(f"[boot] Launching game {appId} via Steam URI.")
        if not sys.platform.startswith("win"):
            try:
                subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
            except Exception as error:
                logError("[boot] Failed to launch game", error)
            return

        try:
            import ctypes
            shell = ctypes.windll.shell32
            shell.ShellExecuteW.restype = ctypes.c_void_p
            shell.ShellExecuteW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
            ]
            hideFlag = 0
            result = shell.ShellExecuteW(None, "open", uri, None, None, hideFlag)
            if result is None or int(result) > 32:
                logInfo("[boot] Game launch dispatched via ShellExecuteW (SW_HIDE, no window).")
                return
            logInfo(f"[boot] ShellExecuteW returned {result}; falling back.")
        except Exception as error:
            logInfo(f"[boot] ShellExecuteW failed ({error}); falling back.")

        try:
            os.startfile(uri)
            logInfo("[boot] Game launch dispatched via os.startfile fallback.")
        except Exception as error:
            logError("[boot] All game-launch methods failed", error)

    def closeGame(self) -> bool:
        self.markBusy(safetyClearAfter=120.0)
        return self.ensureClosed(
            self.config("task_process_name", "TaskBarHero.exe"),
            timeout=self.config("force_kill_timeout_seconds", 10),
        )

    def foregroundIsOurs(self, handle: int) -> bool:
        if not handle:
            return False
        pid = getWindowPid(handle)
        name = ""
        executable = ""
        try:
            process = psutil.Process(pid)
            name = process.name().lower()
            executable = (process.exe() or "").lower()
        except Exception:
            return False
        try:
            sandbox = str(self.speedy.sandboxDir.resolve()).lower()
            if executable and executable.startswith(sandbox):
                return True
        except Exception:
            pass
        return any(keyword in name for keyword in focusThiefKeywords)

    def focusShield(self, protectedHandle: int) -> None:
        try:
            self.focusShieldImpl(protectedHandle)
        except Exception as error:
            logError("Focus shield crashed", error, where="boot.focusShield")

    def focusShieldImpl(self, protectedHandle: int) -> None:
        if not self.config("focus_shield_enabled", True):
            logInfo("[boot] Focus shield disabled via config; skipping.")
            return
        if not protectedHandle:
            return

        watchSeconds = max(0.0, float(self.config("focus_shield_watch_seconds", 15.0)))
        pollSeconds = max(0.1, float(self.config("focus_shield_poll_seconds", 0.5)))
        cooldownSeconds = max(0.0, float(self.config("focus_shield_restore_cooldown_seconds", 1.0)))
        logInfo(f"[boot] Focus shield armed for HWND {protectedHandle} "
                f"(watch {watchSeconds:.1f}s, poll {pollSeconds:.2f}s).")

        deadline = time.time() + watchSeconds
        restores = 0
        nextAllowedRestore = 0.0
        while time.time() < deadline:
            if self.stopEvent.is_set():
                return
            foreground = getForegroundWindow()
            if foreground and foreground != protectedHandle:
                if not self.foregroundIsOurs(foreground):
                    logInfo("[boot] Focus shield: foreground belongs to the user; yielding.")
                    return
                now = time.time()
                if now >= nextAllowedRestore:
                    restored = setForeground(protectedHandle)
                    restores += 1
                    nextAllowedRestore = now + cooldownSeconds
                    logInfo(f"[boot] Focus shield: restore #{restores} to original window "
                            f"({'ok' if restored else 'declined by OS'}).")
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            sleepFor = min(pollSeconds, remaining)
            slept = 0.0
            while slept < sleepFor:
                if self.stopEvent.is_set():
                    return
                chunk = min(0.1, sleepFor - slept)
                time.sleep(chunk)
                slept += chunk

        if restores == 0:
            logInfo("[boot] Focus shield: focus stayed on the original window; no restores needed.")
        else:
            logInfo(f"[boot] Focus shield: watch window ended ({restores} restore(s) performed).")

    def resolveBuild(self, gameModules=None) -> Optional[SpeedyBuild]:
        if self.config("dll_reroller_enabled", True):
            logInfo("[boot] DLL re-roller enabled; requesting fresh camouflage build.")
            self.closeSpeedyAndHelpers(timeout=3)
            build = self.speedy.prepare(gameModules=gameModules)
            if build:
                return build
            logInfo("[boot] Re-roll failed; falling back to existing build.")
        return self.speedy.findActiveBuild()

    def launchSpeedy(self, build: Optional[SpeedyBuild]) -> bool:
        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = swHide
        except Exception:
            startup = None

        if build and build.isRunnable():
            command = [str(build.speedyExe)]
            workingDir = str(build.sandboxDir)
            logInfo(f"[boot] Launching camouflaged Speedy from temp sandbox '{workingDir}' "
                    f"(exe '{build.speedyExe}', injected DLL '{build.dll64}').")
        else:
            command = ["powershell", "-NoProfile", "-Command",
                       self.config("speedy_command", "speedy")]
            workingDir = None
            logInfo("[boot] Launching Speedy via fallback PowerShell command.")

        try:
            process = subprocess.Popen(
                command,
                creationflags=detachedProcess,
                startupinfo=startup,
                cwd=workingDir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            try:
                del process
            except Exception:
                pass
            return True
        except Exception as error:
            logError("[boot] Failed to start Speedy", error)
            return False

    def runSpeedyWorkflow(self) -> None:
        try:
            self.runSpeedyWorkflowImpl()
        except Exception as error:
            logError("Speedy workflow crashed", error=error, where="boot.runSpeedyWorkflow")
            self.state.setFields(dllStatus="Workflow error (see log)")
            try:
                self.closeSpeedyAndHelpers(timeout=3)
            except Exception:
                pass

    def runSpeedyWorkflowImpl(self) -> None:
        gameName = self.config("task_process_name", "TaskBarHero.exe")

        self.state.setFields(dllStatus="Waiting for game process")
        guardTimeout = self.config("game_launch_timeout_seconds", 120)
        deadline = time.time() + guardTimeout
        gameProcessPid = 0
        while time.time() < deadline:
            running = self.processesNamed(gameName)
            if running:
                gameProcessPid = running[0].pid
                break
            if self.stopEvent.is_set():
                return
            time.sleep(1)
        else:
            logInfo(f"[boot] Speedy workflow: {gameName} never appeared within "
                    f"{guardTimeout}s; skipping Speedy so the game is not crashed.")
            self.state.setFields(dllStatus="Skipped (game not detected)")
            return

        baseDelay = self.config("speedy_delay_seconds", 7.0)
        jitter = random.uniform(0.0, self.config("speedy_delay_jitter_seconds", 2.0))
        delay = baseDelay + jitter
        self.state.setFields(dllStatus="Waiting for loader")
        logInfo(f"[boot] Speedy workflow: game confirmed; waiting {delay:.2f}s "
                f"(base {baseDelay}s + jitter {jitter:.2f}s) for loading screen.")
        time.sleep(delay)

        if not self.processesNamed(gameName):
            logInfo("[boot] Speedy workflow: game vanished during the wait; aborting Speedy.")
            self.state.setFields(dllStatus="Aborted (game closed)")
            return

        loadedPaths = set()
        try:
            loadedPaths = self.loadedDllPaths(gameProcessPid)
        except Exception:
            pass
        gameModules = {Path(path).name for path in loadedPaths}

        alreadyInjected, foundDll = self.isAlreadyInjected(gameProcessPid)
        if alreadyInjected:
            logInfo(f"[boot] Speedy workflow: already injected (module '{foundDll}' "
                    f"present); skipping to avoid double injection.")
            self.state.setFields(dllStatus=f"Already injected ({foundDll})")
            return

        build = self.resolveBuild(gameModules=gameModules)
        targetDll = build.dll64 if build else "speedpatch64.dll"

        if not self.injectOnce(build, targetDll):
            return
        self.speedy.markWorking(build)
        self.state.setFields(dllStatus=f"Done ({targetDll})")

        if self.config("verify_injection_enabled", True):
            self.verifyAndMaybeReinject(gameProcessPid, gameName, build, targetDll)
        else:
            self.notifyInjection()

    def notifyInjection(self) -> None:
        if self.desktopExile is not None:
            try:
                self.desktopExile.signalInjectionConfirmed()
            except Exception as error:
                logInfo(f"[boot] Could not signal injection to desktop exile: {error}")
        if self.popupSuppressor is not None:
            try:
                self.popupSuppressor.stop()
                self.popupSuppressor.restoreHidden()
            except Exception as error:
                logInfo(f"[boot] Could not stop/restore popup suppressor: {error}")

    def injectOnce(self, build, targetDll: str) -> bool:
        launcherName = build.speedyExe.name if build else "Speedy.exe"
        self.state.setFields(dllStatus=f"Initializing ({targetDll})")
        logInfo(f"[boot] Speedy workflow: launching '{launcherName}' silently "
                f"(injected DLL '{targetDll}').")
        if not self.launchSpeedy(build):
            self.state.setFields(dllStatus="Launch failed")
            return False

        dwellDuration = self.config("speedy_init_seconds", 3.0) + random.random()
        logInfo(f"[boot] Speedy workflow: dwelling a randomized {dwellDuration:.4f}s "
                f"while suppressing windows.")
        hideNames = {launcherName, "Speedy.exe"}
        if build:
            hideNames.update({build.bridge64, build.bridge32})
        deadline = time.time() + dwellDuration
        while time.time() < deadline:
            for hideName in hideNames:
                self.hideProcessWindows(hideName)
            time.sleep(0.05)

        logInfo("[boot] Speedy workflow: terminating launcher + bridges after initialization.")
        try:
            self.closeWindowsInDir(self.speedy.sandboxDir)
        except Exception as error:
            logInfo(f"[boot] window-close (pre-terminate) error: {error}")
        self.closeSpeedyAndHelpers(timeout=5, build=build)
        return True

    def verifyAndMaybeReinject(self, gamePid: int, gameName: str, build, targetDll: str) -> None:
        delay = self.config("verify_injection_delay_seconds", 5.0)
        logInfo(f"[boot] Speedy workflow: verifying injection in {delay:.0f}s...")
        time.sleep(delay)

        if not self.processesNamed(gameName):
            logInfo("[boot] Verify: game no longer running; nothing to verify.")
            self.notifyInjection()
            return

        if not self.loadedDllPaths(gamePid):
            logInfo("[boot] Verify: cannot read game modules (no admin?); skipping verify.")
            self.state.setFields(dllStatus=f"Done ({targetDll}, unverified)")
            self.notifyInjection()
            return

        injected, found = self.isAlreadyInjected(gamePid)
        if injected:
            logInfo(f"[boot] Verify: injection confirmed (module '{found}' present).")
            self.state.setFields(dllStatus=f"Injected & verified ({targetDll})")
            self.notifyInjection()
            return

        logInfo("[boot] Verify: injection did NOT take; performing one automatic reinject.")
        self.state.setFields(dllStatus=f"Reinjecting ({targetDll})")
        if self.injectOnce(build, targetDll):
            self.speedy.markWorking(build)
        time.sleep(delay)
        reinjected, reinjectedName = self.isAlreadyInjected(gamePid)
        if reinjected:
            logInfo(f"[boot] Verify: reinject succeeded (module '{reinjectedName}').")
            self.state.setFields(dllStatus=f"Injected after reinject ({targetDll})")
            self.notifyInjection()
        else:
            logInfo("[boot] Verify: reinject still not confirmed; leaving as-is.")
            self.state.setFields(dllStatus=f"Reinject unconfirmed ({targetDll})")

    def executeLaunchSequence(self) -> None:
        background = None
        exileActive = (self.desktopExile is not None
                       and self.desktopExile.desktopWorkRequested())
        if (self.popupSuppressor is not None
                and self.config("popup_suppressor_enabled", True)):
            try:
                self.popupSuppressor.reset()
                self.popupSuppressor.start(
                    self.config("popup_suppressor_seconds", 45.0))
            except Exception as error:
                logInfo(f"[boot] Could not start popup suppressor: {error}")
        if exileActive:
            try:
                self.desktopExile.reset()
                self.desktopExile.rememberForeground()
                background = self.desktopExile.prelaunchExile()
            except Exception as error:
                logInfo(f"[boot] Could not pre-exile Steam: {error}")

        if self.config("focus_shield_enabled", False):
            try:
                active = getForegroundWindow()
                if active:
                    threading.Thread(
                        target=self.focusShield,
                        args=(active,),
                        daemon=True,
                    ).start()
            except Exception as error:
                logInfo(f"[boot] Could not start focus shield: {error}")

        logInfo("[boot] Launch sequence: booting game.")
        switched = False
        if exileActive:
            try:
                if self.desktopExile.switchLaunchEnabled():
                    switched = self.desktopExile.switchLaunch(background, self.launchGame)
            except Exception as error:
                logInfo(f"[boot] switch-launch failed ({error}); launching normally.")
                switched = False
        if not switched:
            self.launchGame()

        if exileActive:
            try:
                threading.Thread(
                    target=self.desktopExile.run,
                    args=(background,),
                    daemon=True,
                ).start()
            except Exception as error:
                logInfo(f"[boot] Could not start desktop exile watch: {error}")

        if self.config("log_own_windows", True):
            timer = threading.Timer(4.0, self.logOwnWindows)
            timer.daemon = True
            timer.start()

        if self.config("speedy_enabled", True):
            threading.Thread(target=self.runSpeedyWorkflow, daemon=True).start()

    def logOwnWindows(self) -> None:
        try:
            windows = diagnoseOwnWindows(os.getpid())
            logInfo(f"[boot] OWN-WINDOWS diagnostic: this process (pid {os.getpid()}) "
                    f"owns {len(windows)} top-level window(s):")
            for window in windows:
                logInfo(f"[boot]   - hwnd={window['hwnd']} taskbar={window['likely_taskbar']} "
                        f"visible={window['visible']} tool={window['toolwindow']} app={window['appwindow']} "
                        f"owned={window['has_owner']} class='{window['class']}' title='{window['title']}'")
            flagged = [window for window in windows if window["likely_taskbar"]]
            if flagged:
                logInfo(f"[boot] OWN-WINDOWS: {len(flagged)} window(s) would show on the "
                        f"taskbar -> these are the stray entries.")
                if self.config("strip_own_taskbar_windows", True):
                    for window in flagged:
                        if stripFromTaskbar(window["hwnd"]):
                            logInfo(f"[boot]   stripped stray taskbar window hwnd={window['hwnd']} "
                                    f"class='{window['class']}' title='{window['title']}'")
            else:
                logInfo("[boot] OWN-WINDOWS: none of our windows should appear on the taskbar.")
        except Exception as error:
            logInfo(f"[boot] OWN-WINDOWS diagnostic failed: {error}")

    def waitForGameOpen(self, before: set, timeout: Optional[int] = None,
                        processName: Optional[str] = None) -> bool:
        timeout = timeout or self.config("game_launch_timeout_seconds", 120)
        processName = (processName
                       or self.config("game_process_name", None)
                       or self.config("task_process_name", "TaskBarHero.exe"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.processesNamed(processName):
                return True
            if self.stopEvent.is_set():
                return False
            time.sleep(1)
        return False

    def launchGameFlow(self) -> None:
        if not self.launchLock.acquire(blocking=False):
            logInfo("[boot] launchGameFlow: another launch is in progress; skipping.")
            self.clearBusy()
            return
        self.markBusy(safetyClearAfter=120.0)
        try:
            logInfo("[boot] launchGameFlow: starting launch flow.")
            before = self.pidSnapshot()
            self.state.setFields(currentTask="Launching game", gameState="Launching",
                                 lastEvent="Launch requested")
            self.executeLaunchSequence()
            opened = self.waitForGameOpen(before)
            if opened:
                logInfo("[boot] launchGameFlow: game open confirmed.")
                self.state.setFields(currentTask="Monitoring TBH", gameState="Open",
                                     lastEvent="Game appears open")
            else:
                logInfo("[boot] launchGameFlow: could not confirm game open within timeout.")
                self.state.setFields(currentTask="Monitoring TBH", gameState="Not confirmed",
                                     lastEvent="Could not confirm game open")
        finally:
            self.launchLock.release()
            clearTimer = threading.Timer(8.0, self.clearBusy)
            clearTimer.daemon = True
            clearTimer.start()

    def handleHang(self) -> None:
        name = self.config("task_process_name", "TaskBarHero.exe")
        logInfo(f"[boot] {name} hung; closing and relaunching.")
        self.state.setFields(currentTask="Closing TBH", tbhState="No, hung",
                             lastEvent=f"{name} stopped responding")
        if self.closeGame():
            self.state.setFields(lastEvent=f"{name} closed")
        else:
            logInfo(f"[boot] {name} may still be lingering.")
        self.launchGameFlow()

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        issues: list[str] = []

        try:
            count = len(self.processesNamed(self.config("task_process_name", "TaskBarHero.exe")))
            report("boot.process_scan", "PASS", f"{count} TBH process(es) seen")
        except Exception as error:
            report("boot.process_scan", "FAIL", str(error))
            issues.append("process scan raised")

        try:
            snapshot = self.pidSnapshot()
            report("boot.pid_snapshot", "PASS" if snapshot else "INFO", f"{len(snapshot)} pids")
        except Exception as error:
            report("boot.pid_snapshot", "FAIL", str(error))
            issues.append("pid snapshot raised")

        try:
            running = self.isGameRunning()
            report("boot.is_game_running", "PASS", str(running))
        except Exception as error:
            report("boot.is_game_running", "FAIL", str(error))
            issues.append("isGameRunning raised")

        try:
            hung = self.isGameHung()
            report("boot.is_game_hung", "PASS", str(hung))
        except Exception as error:
            report("boot.is_game_hung", "FAIL", str(error))
            issues.append("isGameHung raised")

        try:
            build = self.speedy.findActiveBuild()
            report("boot.speedy_link", "PASS",
                   f"active build: {build.dll64}" if build else "no active build yet")
        except Exception as error:
            report("boot.speedy_link", "FAIL", str(error))
            issues.append("speedy link raised")

        return issues
