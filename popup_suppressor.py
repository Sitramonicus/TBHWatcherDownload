from __future__ import annotations

import ctypes
import threading
import time
from typing import Callable, Optional

import psutil

from watcher_config import logInfo, logError, getWindowPid, user32, minimiseWindow, closeWindow

try:
    import ctypes.wintypes as wintypes
    ole32 = ctypes.windll.ole32
except Exception:
    wintypes = None
    ole32 = None

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
EVENT_OBJECT_SHOW = 0x8002
OBJID_WINDOW = 0
SW_HIDE = 0
WM_QUIT = 0x0012


def buildEventProcType():
    if wintypes is None:
        return None
    return ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )


class PopupSuppressor:
    def __init__(self, configProvider: Callable, ownPid: Optional[int] = None):
        self.configProvider = configProvider
        self.ownPid = ownPid if ownPid is not None else 0
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None
        self.threadId = 0
        self.running = False
        self.hookHandle = 0
        self.callbackRef = None
        self.hiddenHandles = set()
        self.stopAt = 0.0

    def config(self, key, default=None):
        return getattr(self.configProvider(), key, default)

    def available(self) -> bool:
        return user32 is not None and wintypes is not None

    def windowTitle(self, hwnd: int) -> str:
        if user32 is None:
            return ""
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            return buffer.value
        except Exception:
            return ""

    def windowClass(self, hwnd: int) -> str:
        if user32 is None:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buffer, 256)
            return buffer.value
        except Exception:
            return ""

    def processName(self, hwnd: int) -> str:
        try:
            pid = getWindowPid(hwnd)
            if not pid:
                return ""
            return psutil.Process(pid).name().lower()
        except Exception:
            return ""

    def isMainSteamClient(self, className: str) -> bool:
        return className.lower() in ("sdl_app",)

    def shouldHide(self, hwnd: int) -> bool:
        if self.ownPid and getWindowPid(hwnd) == self.ownPid:
            return False
        processName = self.processName(hwnd)
        title = self.windowTitle(hwnd)
        className = self.windowClass(hwnd)
        loweredTitle = title.lower()
        if processName == "steam.exe":
            if self.isMainSteamClient(className):
                return False
            return True
        for hint in ("launching", "preparing to launch", "preparing steam"):
            if hint in loweredTitle:
                return True
        return False

    def hideWindowHandle(self, hwnd: int) -> None:
        if user32 is None:
            return
        try:
            user32.ShowWindow(hwnd, SW_HIDE)
            with self.lock:
                self.hiddenHandles.add(hwnd)
            logInfo(f"[popup] Instantly hid Steam popup '{self.windowTitle(hwnd)}' "
                    f"(class '{self.windowClass(hwnd)}').")
        except Exception as error:
            logInfo(f"[popup] ShowWindow(hide) failed for {hwnd}: {error}")

    def onEvent(self, hookHandle, event, hwnd, idObject, idChild, threadId, timeMs):
        try:
            if idObject != OBJID_WINDOW:
                return
            if not hwnd:
                return
            if self.stopAt and time.time() > self.stopAt:
                return
            if self.shouldHide(hwnd):
                self.hideWindowHandle(hwnd)
        except Exception:
            pass

    def start(self, durationSeconds: float) -> bool:
        with self.lock:
            if self.running:
                self.stopAt = time.time() + max(0.0, durationSeconds)
                logInfo("[popup] Suppressor already running; extended window.")
                return True
            if not self.available():
                logInfo("[popup] Win32 unavailable; popup suppressor disabled.")
                return False
            self.stopAt = time.time() + max(0.0, durationSeconds)
            self.thread = threading.Thread(target=self.runLoop, daemon=True)
            self.thread.start()
            return True

    def runLoop(self) -> None:
        try:
            self.runLoopImpl()
        except Exception as error:
            logError("Popup suppressor loop crashed", error, where="popup_suppressor.runLoop")
        finally:
            self.running = False

    def runLoopImpl(self) -> None:
        if ole32 is not None:
            try:
                ole32.CoInitialize(0)
            except Exception:
                pass

        eventProcType = buildEventProcType()
        if eventProcType is None:
            return
        self.callbackRef = eventProcType(self.onEvent)

        try:
            self.threadId = ctypes.windll.kernel32.GetCurrentThreadId()
        except Exception:
            self.threadId = 0

        self.hookHandle = user32.SetWinEventHook(
            EVENT_OBJECT_SHOW,
            EVENT_OBJECT_SHOW,
            0,
            self.callbackRef,
            0,
            0,
            WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
        )
        if not self.hookHandle:
            logInfo("[popup] Failed to install EVENT_OBJECT_SHOW hook.")
            if ole32 is not None:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass
            return

        self.running = True
        logInfo(f"[popup] Suppressor active (instant-hide hook) until "
                f"{max(0.0, self.stopAt - time.time()):.0f}s from now.")

        message = wintypes.MSG()
        while True:
            if self.stopAt and time.time() > self.stopAt:
                break
            result = user32.PeekMessageW(ctypes.byref(message), 0, 0, 0, 1)
            if result:
                if message.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            else:
                time.sleep(0.005)

        try:
            user32.UnhookWinEvent(self.hookHandle)
        except Exception:
            pass
        self.hookHandle = 0
        self.callbackRef = None
        if self.hiddenHandles and self.config("popup_suppressor_restore_on_stop", True):
            logInfo("[popup] Suppressor window ended with windows still hidden; restoring.")
            self.restoreHidden()
        if ole32 is not None:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
        logInfo("[popup] Suppressor stopped.")

    def stop(self) -> None:
        self.stopAt = time.time()
        threadId = self.threadId
        if threadId:
            try:
                ctypes.windll.user32.PostThreadMessageW(threadId, WM_QUIT, 0, 0)
            except Exception:
                pass

    def windowExists(self, hwnd: int) -> bool:
        if user32 is None or not hwnd:
            return False
        try:
            return bool(user32.IsWindow(hwnd))
        except Exception:
            return False

    def restoreHidden(self) -> int:
        toTray = bool(self.config("desktop_exile_steam_to_tray", True))
        with self.lock:
            handles = list(self.hiddenHandles)
            self.hiddenHandles = set()
        restored = 0
        for hwnd in handles:
            try:
                if not self.windowExists(hwnd):
                    continue
                if user32 is not None:
                    try:
                        user32.ShowWindow(hwnd, 9)
                    except Exception:
                        pass
                if toTray:
                    closeWindow(hwnd)
                else:
                    minimiseWindow(hwnd)
                restored += 1
            except Exception as error:
                logInfo(f"[popup] Restore of hidden Steam hwnd {hwnd} failed: {error}")
        if restored:
            logInfo(f"[popup] Restored {restored} hidden Steam window(s) "
                    f"({'tray' if toTray else 'minimised'}).")
        return restored

    def takeHiddenHandles(self) -> set:
        handles = set(self.hiddenHandles)
        self.hiddenHandles = set()
        return handles

    def reset(self) -> None:
        self.hiddenHandles = set()

    def diagnose(self, report) -> list:
        issues = []
        report("popup.win32_available", "OK" if self.available() else "WARN",
               "user32+wintypes present" if self.available() else "win32 unavailable")
        report("popup.running", "OK", f"running={self.running}")
        return issues
