from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional


def resolveBaseDir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    nuitkaParent = _nuitkaBundleParent()
    if nuitkaParent is not None:
        return nuitkaParent
    return Path(__file__).resolve().parent


def _nuitkaBundleParent() -> Optional[Path]:
    if "__compiled__" in globals():
        try:
            return Path(sys.argv[0]).resolve().parent
        except Exception:
            return None
    return None


def resolveBundleDir() -> Path:
    pyInstallerExtract = getattr(sys, "_MEIPASS", None)
    if pyInstallerExtract:
        return Path(pyInstallerExtract)
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return resolveBaseDir()


def resolveDataDir() -> Path:
    localAppData = os.environ.get("LOCALAPPDATA")
    if localAppData:
        candidate = Path(localAppData) / "TBHWatcher"
    else:
        home = os.environ.get("HOME") or str(Path.home())
        candidate = Path(home) / ".local" / "share" / "TBHWatcher"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except Exception:
        return resolveBaseDir()


baseDir: Path = resolveBaseDir()
bundleDir: Path = resolveBundleDir()
dataDir: Path = resolveDataDir()
configFile: Path = dataDir / "TBHWatcher_config.json"
logFile: Path = dataDir / "TBHWatcher.log"
tempRootPrefix: str = "_MEI_steam_"


defaultConfig: dict = {
    "task_process_name": "TaskBarHero.exe",
    "game_app_id": "3678970",
    "game_process_name": None,
    "refresher_enabled": True,
    "refresher_interval_minutes": 7.0,
    "refresher_cooldown_seconds": 90,
    "poll_interval_seconds": 2,
    "hang_confirm_seconds": 3,
    "game_launch_timeout_seconds": 120,
    "force_kill_timeout_seconds": 10,
    "speedy_enabled": True,
    "speedy_process_name": "Speedy.exe",
    "speedy_command": "speedy",
    "speedy_delay_seconds": 7.0,
    "speedy_delay_jitter_seconds": 2.0,
    "speedy_init_seconds": 3.0,
    "refresher_interval_jitter_seconds": 20.0,
    "popup_suppressor_enabled": True,
    "popup_suppressor_seconds": 45.0,
    "popup_suppressor_restore_on_stop": True,
    "desktop_exile_enabled": False,
    "desktop_exile_game_title": "Task Bar Hero",
    "desktop_exile_game_enabled": False,
    "desktop_exile_game_to_hidden": True,
    "desktop_exile_minimise_game": True,
    "desktop_exile_prelaunch_steam": True,
    "desktop_exile_hide_popup": True,
    "desktop_exile_steam_to_tray": True,
    "desktop_exile_restore_focus": True,
    "desktop_exile_switch_launch": False,
    "desktop_exile_switch_back_seconds": 1.5,
    "desktop_exile_fast_seconds": 8.0,
    "desktop_exile_fast_poll_seconds": 0.05,
    "desktop_exile_watch_seconds": 40.0,
    "desktop_exile_poll_seconds": 0.5,
    "focus_shield_enabled": False,
    "focus_shield_watch_seconds": 15.0,
    "focus_shield_poll_seconds": 0.5,
    "focus_shield_restore_cooldown_seconds": 1.0,
    "dll_reroller_enabled": True,
    "dll_mutation_pefile_enabled": False,
    "splash_enabled": True,
    "log_own_windows": True,
    "strip_own_taskbar_windows": True,
    "verify_injection_enabled": True,
    "verify_injection_delay_seconds": 5.0,
}


def buildLogger() -> logging.Logger:
    instance = logging.getLogger("TBHWatcher")
    instance.setLevel(logging.INFO)
    if not instance.handlers:
        handler = RotatingFileHandler(
            str(logFile),
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        instance.addHandler(handler)
    return instance


logger: logging.Logger = buildLogger()


def logInfo(message: str) -> None:
    logger.info(message)


errorHints = {
    "PermissionError": "Access denied -- try running as Administrator, or a file is locked by another process.",
    "FileNotFoundError": "A required file/path is missing -- check the OpenSpeedy install or temp store.",
    "OSError": "OS/filesystem issue -- disk full, path too long, or a handle is still open.",
    "psutil.AccessDenied": "Need elevation to read that process -- run as Administrator.",
    "psutil.NoSuchProcess": "Target process exited before we could act on it (usually harmless).",
    "MemoryError": "Out of memory.",
    "ImportError": "A dependency is missing -- reinstall psutil/pystray/pillow/pefile.",
    "ModuleNotFoundError": "A dependency is missing -- reinstall psutil/pystray/pillow/pefile.",
}


def hintFor(error: BaseException) -> str:
    shortName = type(error).__name__
    fullName = f"{type(error).__module__}.{shortName}"
    return errorHints.get(fullName, errorHints.get(shortName, "No specific hint; see traceback above."))


def callerModule() -> str:
    import inspect
    try:
        frame = inspect.stack()[2]
        module = inspect.getmodule(frame[0])
        moduleName = getattr(module, "__name__", None)
        functionName = frame.function
        return f"{moduleName}.{functionName}" if moduleName else functionName
    except Exception:
        return "unknown"


def logError(message: str, error: Optional[BaseException] = None, where: str = "") -> None:
    import traceback
    location = where or callerModule()
    if error is not None:
        hint = hintFor(error)
        logger.error(f"[ERROR] in {location}: {message} | type={type(error).__name__} | hint: {hint}")
        logger.error("Traceback:\n" + "".join(
            traceback.format_exception(type(error), error, error.__traceback__)))
    else:
        logger.exception(f"[ERROR] in {location}: {message}")


singleInstanceHandle = None
mutexName = "Global\\TBHWatcher_SingleInstance_Mutex"


def acquireSingleInstance() -> bool:
    global singleInstanceHandle
    try:
        if sys.platform.startswith("win"):
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            handle = kernel.CreateMutexW(None, 1, mutexName)
            lastError = ctypes.get_last_error()
            alreadyExists = 183
            if not handle:
                logInfo("[main] Single-instance: could not create mutex; allowing start.")
                return True
            if lastError == alreadyExists:
                logInfo("[main] Single-instance: another watcher is already running.")
                return False
            singleInstanceHandle = handle
            return True
    except Exception as error:
        logInfo(f"[main] Single-instance mutex check failed ({error}); using lockfile.")

    try:
        lockPath = dataDir / "TBHWatcher.lock"
        if lockPath.exists():
            try:
                import psutil
                previousPid = int(lockPath.read_text(encoding="utf-8").strip() or "0")
                if previousPid and psutil.pid_exists(previousPid):
                    logInfo(f"[main] Single-instance: live instance pid {previousPid} holds the lock.")
                    return False
            except Exception:
                pass
        lockPath.write_text(str(os.getpid()), encoding="utf-8")
        singleInstanceHandle = lockPath
        return True
    except Exception as error:
        logInfo(f"[main] Single-instance lockfile failed ({error}); allowing start.")
        return True


def warnAlreadyRunning() -> None:
    if user32 is None:
        return
    try:
        iconInformation = 0x00000040
        topMost = 0x00040000
        user32.MessageBoxW(None, "TBH Watcher is already running (check the system tray).",
                           "TBH Watcher", iconInformation | topMost)
    except Exception:
        pass


def installGlobalExceptHook() -> None:
    import traceback

    def handleException(errorType, errorValue, errorTraceback):
        if issubclass(errorType, KeyboardInterrupt):
            return
        hint = hintFor(errorValue) if errorValue else "see traceback"
        logger.error(f"[UNCAUGHT] {errorType.__name__}: {errorValue} | hint: {hint}")
        logger.error("Traceback:\n" + "".join(
            traceback.format_exception(errorType, errorValue, errorTraceback)))

    sys.excepthook = handleException

    def handleThreadException(args):
        handleException(args.exc_type, args.exc_value, args.exc_traceback)

    try:
        threading.excepthook = handleThreadException
    except Exception:
        pass

    logger.info("[main] Global exception hook installed (uncaught errors will be logged).")


def loadConfig() -> dict:
    if configFile.exists():
        try:
            with open(configFile, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            merged = defaultConfig.copy()
            merged.update(stored)
            return merged
        except Exception as error:
            logInfo(f"[config] Failed to parse config, using defaults: {error}")
    return defaultConfig.copy()


def saveConfig(config: dict) -> None:
    temporaryFile = None
    try:
        configFile.parent.mkdir(parents=True, exist_ok=True)
        temporaryFile = configFile.with_suffix(".json.tmp")
        with open(temporaryFile, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporaryFile, configFile)
    except Exception as error:
        logInfo(f"[config] Failed to save config: {error}")
        try:
            if temporaryFile is not None and temporaryFile.exists():
                temporaryFile.unlink()
        except Exception:
            pass


def updateConfigKey(key: str, value) -> dict:
    config = loadConfig()
    config[key] = value
    saveConfig(config)
    return config


def loadUser32():
    try:
        return ctypes.windll.user32
    except Exception:
        return None


user32 = loadUser32()

windowEnumFactory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
EnumWindowsProc = windowEnumFactory(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def enumWindowsForPid(pid: int, onlyVisible: bool = True) -> list[int]:
    windows: list[int] = []
    if user32 is None:
        return windows

    @EnumWindowsProc
    def callback(handle, _lparam):
        try:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == pid and (not onlyVisible or user32.IsWindowVisible(handle)):
                windows.append(handle)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as error:
        logInfo(f"[win32] EnumWindows failed: {error}")
    return windows


def enumAllTopLevelWindows() -> list[tuple]:
    results: list[tuple] = []
    if user32 is None:
        return results

    @EnumWindowsProc
    def callback(handle, _lparam):
        try:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            textLength = user32.GetWindowTextLengthW(handle)
            title = ""
            if textLength > 0:
                buffer = ctypes.create_unicode_buffer(textLength + 1)
                user32.GetWindowTextW(handle, buffer, textLength + 1)
                title = buffer.value
            visible = bool(user32.IsWindowVisible(handle))
            results.append((int(handle), int(owner.value), title, visible))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as error:
        logInfo(f"[win32] EnumWindows(all) failed: {error}")
    return results


def diagnoseOwnWindows(selfPid: int) -> list[dict]:
    found: list[dict] = []
    if user32 is None:
        return found

    styleIndex = -16
    exStyleIndex = -20
    visibleFlag = 0x10000000
    toolWindowFlag = 0x00000080
    appWindowFlag = 0x00040000
    getWindowLong = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW

    @EnumWindowsProc
    def callback(handle, _lparam):
        try:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value != selfPid:
                return True
            textLength = user32.GetWindowTextLengthW(handle)
            title = ""
            if textLength > 0:
                buffer = ctypes.create_unicode_buffer(textLength + 1)
                user32.GetWindowTextW(handle, buffer, textLength + 1)
                title = buffer.value
            classBuffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(handle, classBuffer, 256)
            style = int(getWindowLong(handle, styleIndex))
            exStyle = int(getWindowLong(handle, exStyleIndex))
            ownerHandle = int(user32.GetWindow(handle, 4))
            onTaskbar = (
                bool(style & visibleFlag)
                and ownerHandle == 0
                and (bool(exStyle & appWindowFlag) or not (exStyle & toolWindowFlag))
            )
            found.append({
                "hwnd": int(handle), "title": title, "class": classBuffer.value,
                "visible": bool(style & visibleFlag),
                "toolwindow": bool(exStyle & toolWindowFlag),
                "appwindow": bool(exStyle & appWindowFlag),
                "has_owner": ownerHandle != 0,
                "likely_taskbar": onTaskbar,
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(callback, 0)
    except Exception as error:
        logInfo(f"[win32] diagnoseOwnWindows failed: {error}")
    return found


def stripFromTaskbar(handle: int) -> bool:
    if user32 is None:
        return False
    exStyleIndex = -20
    toolWindowFlag = 0x00000080
    appWindowFlag = 0x00040000
    swHide = 0
    try:
        getLong = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        setLong = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        exStyle = int(getLong(handle, exStyleIndex))
        exStyle = (exStyle | toolWindowFlag) & ~appWindowFlag
        setLong(handle, exStyleIndex, exStyle)
        user32.ShowWindow(handle, swHide)
        return True
    except Exception as error:
        logInfo(f"[win32] stripFromTaskbar failed: {error}")
        return False


def hideWindow(handle: int) -> None:
    if user32 is not None:
        try:
            user32.ShowWindow(handle, 0)
        except Exception as error:
            logInfo(f"[win32] ShowWindow(hide) failed: {error}")


def closeWindow(handle: int) -> None:
    if user32 is not None:
        try:
            user32.PostMessageW(handle, 0x0010, 0, 0)
        except Exception as error:
            logInfo(f"[win32] PostMessage(WM_CLOSE) failed: {error}")


def allowSetForeground() -> None:
    if user32 is None:
        return
    try:
        user32.AllowSetForegroundWindow(-1)
    except Exception as error:
        logInfo(f"[win32] AllowSetForegroundWindow failed: {error}")


def forceForegroundOnce(handle: int) -> bool:
    if user32 is None or not handle:
        return False
    kernel = None
    try:
        kernel = ctypes.windll.kernel32
    except Exception:
        kernel = None
    currentThread = 0
    targetThread = 0
    attached = False
    try:
        try:
            user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        if kernel is not None:
            try:
                currentThread = kernel.GetCurrentThreadId()
                foreground = user32.GetForegroundWindow()
                targetThread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
                if targetThread and targetThread != currentThread:
                    attached = bool(user32.AttachThreadInput(currentThread, targetThread, True))
            except Exception:
                attached = False
        result = bool(user32.SetForegroundWindow(handle))
        return result
    except Exception as error:
        logInfo(f"[win32] forceForegroundOnce failed: {error}")
        return False
    finally:
        if attached and currentThread and targetThread:
            try:
                user32.AttachThreadInput(currentThread, targetThread, False)
            except Exception:
                pass


def stopFlashing(handle: int) -> None:
    if user32 is None or not handle:
        return
    try:
        info = ctypes.Structure
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]
        flash = FLASHWINFO()
        flash.cbSize = ctypes.sizeof(FLASHWINFO)
        flash.hwnd = ctypes.c_void_p(handle)
        flash.dwFlags = 0
        flash.uCount = 0
        flash.dwTimeout = 0
        user32.FlashWindowEx(ctypes.byref(flash))
    except Exception as error:
        logInfo(f"[win32] stopFlashing failed: {error}")


def setForeground(handle: int) -> bool:
    if user32 is None or not handle:
        return False
    try:
        return bool(user32.SetForegroundWindow(handle))
    except Exception as error:
        logInfo(f"[win32] setForeground failed: {error}")
        return False


def minimiseWindow(handle: int) -> bool:
    if user32 is None or not handle:
        return False
    try:
        return bool(user32.ShowWindow(handle, 6))
    except Exception as error:
        logInfo(f"[win32] minimiseWindow failed: {error}")
        return False


def confirmDialog(title: str, message: str) -> bool:
    if user32 is None:
        logInfo("[win32] confirmDialog: no GUI available; treating as No.")
        return False
    try:
        messageBox = user32.MessageBoxW
        messageBox.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        messageBox.restype = ctypes.c_int
        yesNo = 0x00000004
        iconWarning = 0x00000030
        topMost = 0x00040000
        setForeground = 0x00010000
        idYes = 6
        result = messageBox(None, message, title, yesNo | iconWarning | topMost | setForeground)
        logInfo(f"[win32] confirmDialog '{title}': user clicked code {result} "
                f"({'YES' if result == idYes else 'NO/other'}).")
        return result == idYes
    except Exception as error:
        logInfo(f"[win32] confirmDialog failed ({error}); treating as No.")
        return False


def getForegroundWindow() -> int:
    if user32 is None:
        return 0
    try:
        return int(user32.GetForegroundWindow())
    except Exception:
        return 0


def getWindowPid(handle: int) -> int:
    if user32 is None:
        return 0
    try:
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        return int(owner.value)
    except Exception:
        return 0


def isWindowHung(handle: int) -> bool:
    if user32 is None:
        return False
    try:
        if user32.IsHungAppWindow(handle):
            return True
    except Exception as error:
        logInfo(f"[win32] IsHungAppWindow failed: {error}")
    try:
        result = ctypes.c_ulong()
        windowNull = 0x0000
        abortIfHung = 0x0002
        blockSender = 0x0001
        user32.SendMessageTimeoutW(handle, windowNull, 0, 0,
                                   abortIfHung | blockSender, 1000, ctypes.byref(result))
        if result.value == 0 and user32.IsHungAppWindow(handle):
            return True
    except Exception as error:
        logInfo(f"[win32] SendMessageTimeout failed: {error}")
    return False


@dataclass
class WatchState:
    currentTask: str = "Starting"
    tbhState: str = "Unknown"
    gameState: str = "Unknown"
    refresherStatus: str = "Disabled"
    dllStatus: str = "Idle"
    lastEvent: str = "Idle"
    running: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    def setFields(self, **fields) -> None:
        with self.lock:
            for key, value in fields.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "currentTask": self.currentTask,
                "tbhState": self.tbhState,
                "gameState": self.gameState,
                "refresherStatus": self.refresherStatus,
                "dllStatus": self.dllStatus,
                "lastEvent": self.lastEvent,
                "running": self.running,
            }


def resolveIconFile() -> Path:
    candidates = [baseDir / "ico.ico", bundleDir / "ico.ico"]
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return baseDir / "ico.ico"


iconFile: Path = resolveIconFile()


def buildIconImage():
    from PIL import Image, ImageDraw

    if iconFile.exists():
        try:
            return Image.open(str(iconFile)).convert("RGBA")
        except Exception as error:
            logInfo(f"[icon] Failed to load custom ico.ico ({error}); using default icon.")

    size = 64
    image = Image.new("RGBA", (size, size), (28, 28, 32, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14,
                           outline=(210, 210, 220, 255), width=2)
    draw.rounded_rectangle((10, 10, size - 10, size - 10), radius=10,
                           fill=(52, 86, 120, 255))
    draw.text((16, 20), "TBH", fill=(245, 245, 245, 255))
    return image


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    issues: list[str] = []

    report("config.base_dir", "PASS", str(baseDir))
    report("config.data_dir", "PASS", str(dataDir))

    try:
        config = loadConfig()
        missing = [key for key in defaultConfig if key not in config]
        if missing:
            report("config.load", "FAIL", f"missing keys: {missing}")
            issues.append("config keys missing")
        else:
            report("config.load", "PASS", f"{len(config)} keys loaded")
    except Exception as error:
        report("config.load", "FAIL", str(error))
        issues.append("config load raised")

    try:
        probe = dataDir / "._tbh_write_probe.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report("config.write", "PASS", f"data dir writable ({dataDir})")
    except Exception as error:
        report("config.write", "FAIL", f"data dir not writable: {error}")
        issues.append("data dir not writable")

    report("win32.user32", "PASS" if user32 is not None else "INFO",
           "loaded" if user32 is not None else "unavailable (non-Windows)")

    try:
        state = WatchState()
        state.setFields(lastEvent="diag")
        assert state.snapshot()["lastEvent"] == "diag"
        report("state.shared", "PASS", "read/write round-trip ok")
    except Exception as error:
        report("state.shared", "FAIL", str(error))
        issues.append("shared state broken")

    return issues


if __name__ == "__main__":
    def printer(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    foundIssues = diagnose(printer)
    print("OK" if not foundIssues else f"ISSUES: {foundIssues}")
