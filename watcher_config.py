from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

APP_NAME = "TBHWatcher"
CONFIG_FILENAME = "TBHWatcher_config.json"
LOG_FILENAME = "TBHWatcher.log"
LOCK_FILENAME = "TBHWatcher.lock"

LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3

SINGLE_INSTANCE_MUTEX_NAME = "Global\\TBHWatcher_SingleInstance_Mutex"
ERROR_ALREADY_EXISTS = 183

WM_NULL = 0x0000
WM_CLOSE = 0x0010
SW_HIDE = 0
SW_RESTORE = 9
GW_OWNER = 4
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SMTO_FLAGS = 0x0002 | 0x0001
HANG_PING_TIMEOUT_MS = 1000

MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000
IDYES = 6

ICON_CANVAS_SIZE = 64
ICON_BACKGROUND = (28, 28, 32, 255)
ICON_BORDER_COLOUR = (210, 210, 220, 255)
ICON_FILL_COLOUR = (52, 86, 120, 255)
ICON_TEXT_COLOUR = (245, 245, 245, 255)


def resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidate = Path(local_appdata) / APP_NAME
    else:
        home = os.environ.get("HOME") or str(Path.home())
        candidate = Path(home) / ".local" / "share" / APP_NAME
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except Exception:
        return resolve_base_dir()


BASE_DIR: Path = resolve_base_dir()
DATA_DIR: Path = resolve_data_dir()
CONFIG_FILE: Path = DATA_DIR / CONFIG_FILENAME
LOG_FILE: Path = DATA_DIR / LOG_FILENAME
TEMP_ROOT_PREFIX: str = "_MEI_steam_"


DEFAULT_CONFIG: dict = {
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
    "focus_shield_seconds": 15.0,
    "dll_reroller_enabled": True,
    "dll_mutation_pefile_enabled": False,
    "splash_enabled": True,
    "log_own_windows": True,
    "strip_own_taskbar_windows": True,
    "verify_injection_enabled": True,
    "verify_injection_delay_seconds": 5.0,
}


def build_logger() -> logging.Logger:
    instance = logging.getLogger(APP_NAME)
    instance.setLevel(logging.INFO)
    if not instance.handlers:
        rotating_handler = RotatingFileHandler(
            str(LOG_FILE),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        instance.addHandler(rotating_handler)
    return instance


logger: logging.Logger = build_logger()


def log(message: str) -> None:
    logger.info(message)


ERROR_HINTS = {
    "PermissionError": "Access denied -- try running as Administrator, or a file is locked by another process.",
    "FileNotFoundError": "A required file/path is missing -- check the OpenSpeedy install or temp store.",
    "OSError": "OS/filesystem issue -- disk full, path too long, or a handle is still open.",
    "psutil.AccessDenied": "Need elevation to read that process -- run as Administrator.",
    "psutil.NoSuchProcess": "Target process exited before we could act on it (usually harmless).",
    "MemoryError": "Out of memory.",
    "ImportError": "A dependency is missing -- reinstall psutil/pystray/pillow/pefile.",
    "ModuleNotFoundError": "A dependency is missing -- reinstall psutil/pystray/pillow/pefile.",
}


def _hint_for(error: BaseException) -> str:
    short_name = type(error).__name__
    qualified_name = f"{type(error).__module__}.{short_name}"
    return ERROR_HINTS.get(qualified_name, ERROR_HINTS.get(short_name, "No specific hint; see traceback above."))


def _calling_location() -> str:
    import inspect
    try:
        frame_info = inspect.stack()[2]
        module = inspect.getmodule(frame_info[0])
        module_name = getattr(module, "__name__", None)
        function_name = frame_info.function
        return f"{module_name}.{function_name}" if module_name else function_name
    except Exception:
        return "unknown"


def log_error(message: str, exc: BaseException | None = None, where: str = "") -> None:
    import traceback
    location = where or _calling_location()
    if exc is not None:
        hint = _hint_for(exc)
        logger.error(f"[ERROR] in {location}: {message} | type={type(exc).__name__} | hint: {hint}")
        logger.error("Traceback:\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    else:
        logger.exception(f"[ERROR] in {location}: {message}")


_single_instance_handle = None


def acquire_single_instance() -> bool:
    global _single_instance_handle
    try:
        if sys.platform.startswith("win"):
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            mutex_handle = kernel32.CreateMutexW(None, 1, SINGLE_INSTANCE_MUTEX_NAME)
            last_error = ctypes.get_last_error()
            if not mutex_handle:
                log("[main] Single-instance: could not create mutex; allowing start.")
                return True
            if last_error == ERROR_ALREADY_EXISTS:
                log("[main] Single-instance: another watcher is already running.")
                return False
            _single_instance_handle = mutex_handle
            return True
    except Exception as error:
        log(f"[main] Single-instance mutex check failed ({error}); using lockfile.")

    try:
        lock_file = DATA_DIR / LOCK_FILENAME
        if lock_file.exists():
            try:
                import psutil
                previous_pid = int(lock_file.read_text(encoding="utf-8").strip() or "0")
                if previous_pid and psutil.pid_exists(previous_pid):
                    log(f"[main] Single-instance: live instance pid {previous_pid} holds the lock.")
                    return False
            except Exception:
                pass
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        _single_instance_handle = lock_file
        return True
    except Exception as error:
        log(f"[main] Single-instance lockfile failed ({error}); allowing start.")
        return True


def warn_already_running() -> None:
    if user32 is None:
        return
    try:
        user32.MessageBoxW(None, "TBH Watcher is already running (check the system tray).",
                           "TBH Watcher", MB_ICONINFORMATION | MB_TOPMOST)
    except Exception:
        pass


def install_global_excepthook() -> None:
    import traceback

    def handle_uncaught(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        hint = _hint_for(exc_value) if exc_value else "see traceback"
        logger.error(f"[UNCAUGHT] {exc_type.__name__}: {exc_value} | hint: {hint}")
        logger.error("Traceback:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))

    sys.excepthook = handle_uncaught

    def handle_thread_exception(args):
        handle_uncaught(args.exc_type, args.exc_value, args.exc_traceback)

    try:
        threading.excepthook = handle_thread_exception
    except Exception:
        pass

    logger.info("[main] Global exception hook installed (uncaught errors will be logged).")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            merged = DEFAULT_CONFIG.copy()
            merged.update(stored)
            return merged
        except Exception as error:
            log(f"[config] Failed to parse config, using defaults: {error}")
    return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    temp_file = CONFIG_FILE.with_suffix(".json.tmp")
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file, CONFIG_FILE)
    except Exception as error:
        log(f"[config] Failed to save config: {error}")
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


def update_config_key(key: str, value) -> dict:
    config = load_config()
    config[key] = value
    save_config(config)
    return config


def _load_user32():
    try:
        return ctypes.windll.user32  # type: ignore[attr-defined]
    except Exception:
        return None


user32 = _load_user32()

_WindowEnumeratorType = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
EnumWindowsProc = _WindowEnumeratorType(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def enum_windows_for_pid(pid: int, only_visible: bool = True) -> list[int]:
    windows: list[int] = []
    if user32 is None:
        return windows

    @EnumWindowsProc
    def collect(window_handle, _lparam):
        try:
            owning_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window_handle, ctypes.byref(owning_pid))
            if owning_pid.value == pid and (not only_visible or user32.IsWindowVisible(window_handle)):
                windows.append(window_handle)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(collect, 0)
    except Exception as error:
        log(f"[win32] EnumWindows failed: {error}")
    return windows


def enum_all_top_level_windows() -> list[tuple[int, int, str, bool]]:
    results: list[tuple[int, int, str, bool]] = []
    if user32 is None:
        return results

    @EnumWindowsProc
    def collect(window_handle, _lparam):
        try:
            owning_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window_handle, ctypes.byref(owning_pid))
            title_length = user32.GetWindowTextLengthW(window_handle)
            title = ""
            if title_length > 0:
                title_buffer = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(window_handle, title_buffer, title_length + 1)
                title = title_buffer.value
            is_visible = bool(user32.IsWindowVisible(window_handle))
            results.append((int(window_handle), int(owning_pid.value), title, is_visible))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(collect, 0)
    except Exception as error:
        log(f"[win32] EnumWindows(all) failed: {error}")
    return results


def diagnose_own_windows(self_pid: int) -> list[dict]:
    results: list[dict] = []
    if user32 is None:
        return results

    get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW

    @EnumWindowsProc
    def collect(window_handle, _lparam):
        try:
            owning_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(window_handle, ctypes.byref(owning_pid))
            if owning_pid.value != self_pid:
                return True
            title_length = user32.GetWindowTextLengthW(window_handle)
            title = ""
            if title_length > 0:
                title_buffer = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(window_handle, title_buffer, title_length + 1)
                title = title_buffer.value
            class_buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(window_handle, class_buffer, 256)
            style = int(get_window_long(window_handle, GWL_STYLE))
            extended_style = int(get_window_long(window_handle, GWL_EXSTYLE))
            owner_window = int(user32.GetWindow(window_handle, GW_OWNER))
            shows_on_taskbar = (
                bool(style & WS_VISIBLE)
                and owner_window == 0
                and (bool(extended_style & WS_EX_APPWINDOW) or not (extended_style & WS_EX_TOOLWINDOW))
            )
            results.append({
                "hwnd": int(window_handle), "title": title, "class": class_buffer.value,
                "visible": bool(style & WS_VISIBLE),
                "toolwindow": bool(extended_style & WS_EX_TOOLWINDOW),
                "appwindow": bool(extended_style & WS_EX_APPWINDOW),
                "has_owner": owner_window != 0,
                "likely_taskbar": shows_on_taskbar,
            })
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(collect, 0)
    except Exception as error:
        log(f"[win32] diagnose_own_windows failed: {error}")
    return results


def strip_from_taskbar(hwnd: int) -> bool:
    if user32 is None:
        return False
    try:
        get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_window_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        extended_style = int(get_window_long(hwnd, GWL_EXSTYLE))
        extended_style = (extended_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        set_window_long(hwnd, GWL_EXSTYLE, extended_style)
        user32.ShowWindow(hwnd, SW_HIDE)
        return True
    except Exception as error:
        log(f"[win32] strip_from_taskbar failed: {error}")
        return False


def hide_window(hwnd: int) -> None:
    if user32 is not None:
        try:
            user32.ShowWindow(hwnd, SW_HIDE)
        except Exception as error:
            log(f"[win32] ShowWindow(hide) failed: {error}")


def close_window(hwnd: int) -> None:
    if user32 is not None:
        try:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception as error:
            log(f"[win32] PostMessage(WM_CLOSE) failed: {error}")


def restore_and_focus(hwnd: int) -> None:
    if user32 is not None:
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        except Exception as error:
            log(f"[win32] restore_and_focus failed: {error}")


def confirm_dialog(title: str, message: str) -> bool:
    if user32 is None:
        log("[win32] confirm_dialog: no GUI available; treating as No.")
        return False
    try:
        show_message_box = user32.MessageBoxW
        show_message_box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        show_message_box.restype = ctypes.c_int
        clicked = show_message_box(None, message, title,
                                   MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND)
        log(f"[win32] confirm_dialog '{title}': user clicked code {clicked} "
            f"({'YES' if clicked == IDYES else 'NO/other'}).")
        return clicked == IDYES
    except Exception as error:
        log(f"[win32] confirm_dialog failed ({error}); treating as No.")
        return False


def get_foreground_window() -> int:
    if user32 is None:
        return 0
    try:
        return int(user32.GetForegroundWindow())
    except Exception:
        return 0


def get_window_pid(hwnd: int) -> int:
    if user32 is None:
        return 0
    try:
        owning_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owning_pid))
        return int(owning_pid.value)
    except Exception:
        return 0


def is_window_hung(hwnd: int) -> bool:
    if user32 is None:
        return False
    try:
        if user32.IsHungAppWindow(hwnd):
            return True
    except Exception as error:
        log(f"[win32] IsHungAppWindow failed: {error}")
    try:
        ping_result = ctypes.c_ulong()
        user32.SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_FLAGS, HANG_PING_TIMEOUT_MS, ctypes.byref(ping_result))
        if ping_result.value == 0 and user32.IsHungAppWindow(hwnd):
            return True
    except Exception as error:
        log(f"[win32] SendMessageTimeout failed: {error}")
    return False


@dataclass
class WatchState:
    current_task: str = "Starting"
    tbh_state: str = "Unknown"
    game_state: str = "Unknown"
    refresher_status: str = "Disabled"
    dll_status: str = "Idle"
    last_event: str = "Idle"
    running: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, **fields) -> None:
        with self.lock:
            for key, value in fields.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "current_task": self.current_task,
                "tbh_state": self.tbh_state,
                "game_state": self.game_state,
                "refresher_status": self.refresher_status,
                "dll_status": self.dll_status,
                "last_event": self.last_event,
                "running": self.running,
            }


def _resolve_icon_file() -> Path:
    candidates = [BASE_DIR / "ico.ico"]
    bundle_extract_dir = getattr(sys, "_MEIPASS", None)
    if bundle_extract_dir:
        candidates.append(Path(bundle_extract_dir) / "ico.ico")
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return BASE_DIR / "ico.ico"


ICON_FILE: Path = _resolve_icon_file()


def build_icon_image():
    from PIL import Image, ImageDraw

    if ICON_FILE.exists():
        try:
            return Image.open(str(ICON_FILE)).convert("RGBA")
        except Exception as error:
            log(f"[icon] Failed to load custom ico.ico ({error}); using default icon.")

    size = ICON_CANVAS_SIZE
    image = Image.new("RGBA", (size, size), ICON_BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, outline=ICON_BORDER_COLOUR, width=2)
    draw.rounded_rectangle((10, 10, size - 10, size - 10), radius=10, fill=ICON_FILL_COLOUR)
    draw.text((16, 20), "TBH", fill=ICON_TEXT_COLOUR)
    return image


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    problems: list[str] = []

    report("config.base_dir", "PASS", str(BASE_DIR))
    report("config.data_dir", "PASS", str(DATA_DIR))

    try:
        config = load_config()
        missing_keys = [key for key in DEFAULT_CONFIG if key not in config]
        if missing_keys:
            report("config.load", "FAIL", f"missing keys: {missing_keys}")
            problems.append("config keys missing")
        else:
            report("config.load", "PASS", f"{len(config)} keys loaded")
    except Exception as error:
        report("config.load", "FAIL", str(error))
        problems.append("config load raised")

    try:
        probe_file = DATA_DIR / "._tbh_write_probe.tmp"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        report("config.write", "PASS", f"data dir writable ({DATA_DIR})")
    except Exception as error:
        report("config.write", "FAIL", f"data dir not writable: {error}")
        problems.append("data dir not writable")

    report("win32.user32", "PASS" if user32 is not None else "INFO",
           "loaded" if user32 is not None else "unavailable (non-Windows)")

    try:
        state = WatchState()
        state.set(last_event="diag")
        assert state.snapshot()["last_event"] == "diag"
        report("state.shared", "PASS", "read/write round-trip ok")
    except Exception as error:
        report("state.shared", "FAIL", str(error))
        problems.append("shared state broken")

    return problems


if __name__ == "__main__":
    def _print_row(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    found = diagnose(_print_row)
    print("OK" if not found else f"ISSUES: {found}")
