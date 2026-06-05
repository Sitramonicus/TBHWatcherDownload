from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Set

import psutil

import stealth_temp
from watcher_config import (
    close_window,
    diagnose_own_windows,
    strip_from_taskbar,
    enum_all_top_level_windows,
    enum_windows_for_pid,
    get_foreground_window,
    get_window_pid,
    hide_window,
    is_window_hung,
    log,
    log_error,
    restore_and_focus,
)
from speedy_dll import (
    SpeedyDLLManager,
    SpeedyBuild,
    CAMOUFLAGE_NAMES,
    ORIGINAL_64,
    ORIGINAL_32,
)

INJECTABLE_DLL_NAMES = {ORIGINAL_64.lower(), ORIGINAL_32.lower()}
for _camo_64, _camo_32 in CAMOUFLAGE_NAMES:
    INJECTABLE_DLL_NAMES.add(_camo_64.lower())
    INJECTABLE_DLL_NAMES.add(_camo_32.lower())

SW_HIDE = 0
DETACHED_PROCESS = 0x00000008
SHELL_EXECUTE_SUCCESS_THRESHOLD = 32

FOCUS_THIEF_KEYWORDS = ("steam", "taskbarhero", "speedy", "bridge")

DEFAULT_GAME_PROCESS_NAME = "TaskBarHero.exe"
DEFAULT_GAME_APP_ID = "3678970"
DEFAULT_SPEEDY_PROCESS_NAME = "Speedy.exe"
SPEEDY_BRIDGE_NAMES = ("bridge64.exe", "bridge32.exe")

DEFAULT_KILL_TIMEOUT_SECONDS = 10
QUICK_KILL_TIMEOUT_SECONDS = 3
PROCESS_RECHECK_INTERVAL_SECONDS = 0.5
GENERIC_POLL_INTERVAL_SECONDS = 1
FOCUS_SHIELD_POLL_INTERVAL_SECONDS = 0.05
WINDOW_SUPPRESSION_INTERVAL_SECONDS = 0.05

DEFAULT_LAUNCH_TIMEOUT_SECONDS = 120
DEFAULT_FOCUS_SHIELD_SECONDS = 15.0
DEFAULT_LOADING_SCREEN_WAIT_SECONDS = 7.0
DEFAULT_LOADING_WAIT_JITTER_SECONDS = 2.0
DEFAULT_SPEEDY_INIT_SECONDS = 3.0
DEFAULT_VERIFY_DELAY_SECONDS = 5.0

OWN_WINDOW_DIAGNOSTIC_DELAY_SECONDS = 4.0
BUSY_DEFAULT_SAFETY_SECONDS = 30.0
BUSY_LAUNCH_SAFETY_SECONDS = 120.0
POST_LAUNCH_BUSY_GRACE_SECONDS = 8.0


class GameBooter:
    def __init__(
        self,
        config_provider: Callable[[], object],
        state,
        stop_event: threading.Event,
        speedy_manager: SpeedyDLLManager,
    ) -> None:
        self._read_config = config_provider
        self.state = state
        self.stop_event = stop_event
        self.speedy = speedy_manager
        self._launch_lock = threading.Lock()
        self._intentional_action = threading.Event()
        self._busy_safety_timer: Optional[threading.Timer] = None

    @property
    def busy(self) -> bool:
        return self._intentional_action.is_set()

    def mark_busy(self, safety_clear_after: float = BUSY_DEFAULT_SAFETY_SECONDS) -> None:
        self._intentional_action.set()
        if self._busy_safety_timer is not None:
            try:
                self._busy_safety_timer.cancel()
            except Exception:
                pass
        self._busy_safety_timer = threading.Timer(safety_clear_after, self._intentional_action.clear)
        self._busy_safety_timer.daemon = True
        self._busy_safety_timer.start()

    def clear_busy(self) -> None:
        if self._busy_safety_timer is not None:
            try:
                self._busy_safety_timer.cancel()
            except Exception:
                pass
        self._intentional_action.clear()

    def _setting(self, key, default=None):
        return getattr(self._read_config(), key, default)

    def processes_named(self, name: str) -> list[psutil.Process]:
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if process.info["name"] == name:
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as error:
                log(f"[boot] process_iter error: {error}")
        return matches

    def pid_snapshot(self) -> Set[int]:
        try:
            return {process.pid for process in psutil.process_iter(["pid"])}
        except Exception as error:
            log(f"[boot] pid snapshot error: {error}")
            return set()

    def kill_tree(self, pid: int, timeout: int = DEFAULT_KILL_TIMEOUT_SECONDS) -> bool:
        try:
            root_process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except Exception as error:
            log(f"[boot] kill_tree lookup error: {error}")
            return False

        try:
            child_processes = root_process.children(recursive=True)
        except Exception:
            child_processes = []
        for child in child_processes:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            root_process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        try:
            _, still_alive = psutil.wait_procs([root_process, *child_processes], timeout=timeout)
            for survivor in still_alive:
                try:
                    survivor.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            _, refused_to_die = psutil.wait_procs(still_alive, timeout=timeout)
            return not refused_to_die
        except Exception as error:
            log(f"[boot] kill_tree wait error: {error}")
            return False

    def ensure_closed(self, name: str, timeout: int = DEFAULT_KILL_TIMEOUT_SECONDS) -> bool:
        all_killed = all(self.kill_tree(p.pid, timeout=timeout) for p in self.processes_named(name))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.processes_named(name):
                return True
            time.sleep(PROCESS_RECHECK_INTERVAL_SECONDS)
        return all_killed and not self.processes_named(name)

    def processes_under_dir(self, directory) -> list[psutil.Process]:
        matches: list[psutil.Process] = []
        try:
            target_prefix = str(Path(directory).resolve()).lower()
        except Exception:
            return matches
        for process in psutil.process_iter(["pid", "exe"]):
            try:
                executable = process.info.get("exe")
                if executable and str(Path(executable).resolve()).lower().startswith(target_prefix):
                    matches.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        return matches

    def _our_temp_dirs(self, extra_build=None) -> set:
        directories = set()
        try:
            directories.add(self.speedy.sandbox_dir)
            directories.add(self.speedy.store_dir)
        except Exception:
            pass
        if extra_build is not None:
            try:
                directories.add(extra_build.sandbox_dir)
            except Exception:
                pass
        return directories

    def close_speedy_and_helpers(self, timeout: int = 5, build=None) -> None:
        processes_killed = 0
        for directory in self._our_temp_dirs(extra_build=build):
            for process in self.processes_under_dir(directory):
                if self.kill_tree(process.pid, timeout=timeout):
                    processes_killed += 1
        for name in (self._setting("speedy_process_name", DEFAULT_SPEEDY_PROCESS_NAME),
                     DEFAULT_SPEEDY_PROCESS_NAME, *SPEEDY_BRIDGE_NAMES):
            if name:
                self.ensure_closed(name, timeout=timeout)
        if processes_killed:
            log(f"[boot] Closed {processes_killed} injector process(es) running from our temp folders.")

    def hide_process_windows(self, name: str) -> None:
        for process in self.processes_named(name):
            try:
                for window in enum_windows_for_pid(process.pid, only_visible=True):
                    hide_window(window)
            except Exception as error:
                log(f"[boot] window-hide error for {process.pid}: {error}")

    def close_windows_in_dir(self, directory) -> None:
        for process in self.processes_under_dir(directory):
            try:
                for window in enum_windows_for_pid(process.pid, only_visible=False):
                    close_window(window)
            except Exception as error:
                log(f"[boot] window-close error for {process.pid}: {error}")

    def emergency_window_clear(self) -> dict:
        import tempfile
        summary = {"dir_windows_closed": 0, "procs_killed": 0,
                   "blank_windows_closed": 0, "orphans_swept": 0,
                   "own_stripped": 0}

        try:
            for window in diagnose_own_windows(os.getpid()):
                if window["likely_taskbar"] and strip_from_taskbar(window["hwnd"]):
                    summary["own_stripped"] += 1
        except Exception as error:
            log(f"[boot] emergency: own-window strip error: {error}")

        for directory in self._our_temp_dirs():
            try:
                for process in self.processes_under_dir(directory):
                    try:
                        for window in enum_windows_for_pid(process.pid, only_visible=False):
                            close_window(window)
                            summary["dir_windows_closed"] += 1
                    except Exception:
                        pass
                    if self.kill_tree(process.pid, timeout=QUICK_KILL_TIMEOUT_SECONDS):
                        summary["procs_killed"] += 1
            except Exception as error:
                log(f"[boot] emergency: dir cleanup error: {error}")

        try:
            summary["orphans_swept"] = stealth_temp.sweep_orphans()
        except Exception as error:
            log(f"[boot] emergency: orphan sweep error: {error}")

        try:
            temp_root = str(Path(tempfile.gettempdir()).resolve()).lower()
            for window_handle, owning_pid, title, _visible in enum_all_top_level_windows():
                if title.strip():
                    continue
                try:
                    executable = (psutil.Process(owning_pid).exe() or "").lower()
                except Exception:
                    executable = ""
                if executable and str(Path(executable).resolve()).lower().startswith(temp_root):
                    close_window(window_handle)
                    summary["blank_windows_closed"] += 1
        except Exception as error:
            log(f"[boot] emergency: blank-window scan error: {error}")

        log(f"[boot] Emergency window clear done: {summary}")
        return summary

    def loaded_dll_paths(self, pid: int) -> Set[str]:
        dll_paths: Set[str] = set()
        try:
            process = psutil.Process(pid)
            for mapped_region in process.memory_maps():
                path = mapped_region.path.lower()
                if path.endswith(".dll"):
                    dll_paths.add(path)
        except psutil.AccessDenied:
            log("[boot] DLL detection: access denied reading memory maps (need admin).")
        except Exception as error:
            log(f"[boot] DLL detection error: {error}")
        return dll_paths

    def is_already_injected(self, pid: int) -> tuple[bool, str]:
        try:
            loaded = self.loaded_dll_paths(pid)
        except Exception as error:
            log(f"[boot] inject-check: could not read modules: {error}")
            return (False, "")
        if not loaded:
            return (False, "")
        for full_path in loaded:
            module_name = Path(full_path).name.lower()
            if module_name in INJECTABLE_DLL_NAMES:
                return (True, module_name)
        return (False, "")

    def is_game_running(self) -> bool:
        return bool(self.processes_named(self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME)))

    def is_game_hung(self) -> bool:
        for process in self.processes_named(self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME)):
            try:
                if psutil.Process(process.pid).status() == psutil.STATUS_ZOMBIE:
                    return True
                windows = enum_windows_for_pid(process.pid, only_visible=True)
                if windows and any(is_window_hung(window) for window in windows):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def launch_game(self) -> None:
        app_id = self._setting("game_app_id", DEFAULT_GAME_APP_ID)
        steam_uri = f"steam://rungameid/{app_id}"
        log(f"[boot] Launching game {app_id} via Steam URI.")
        if not sys.platform.startswith("win"):
            try:
                subprocess.Popen(["cmd", "/c", "start", "", steam_uri], shell=False)
            except Exception as error:
                log_error(f"[boot] Failed to launch game: {error}")
            return

        try:
            import ctypes
            shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            shell32.ShellExecuteW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
            ]
            result = shell32.ShellExecuteW(None, "open", steam_uri, None, None, SW_HIDE)
            if result is None or int(result) > SHELL_EXECUTE_SUCCESS_THRESHOLD:
                log("[boot] Game launch dispatched via ShellExecuteW (SW_HIDE, no window).")
                return
            log(f"[boot] ShellExecuteW returned {result}; falling back.")
        except Exception as error:
            log(f"[boot] ShellExecuteW failed ({error}); falling back.")

        try:
            os.startfile(steam_uri)  # type: ignore[attr-defined]
            log("[boot] Game launch dispatched via os.startfile fallback.")
        except Exception as error:
            log_error(f"[boot] All game-launch methods failed: {error}")

    def close_game(self) -> bool:
        self.mark_busy(safety_clear_after=BUSY_LAUNCH_SAFETY_SECONDS)
        return self.ensure_closed(
            self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME),
            timeout=self._setting("force_kill_timeout_seconds", DEFAULT_KILL_TIMEOUT_SECONDS),
        )

    def focus_shield(self, protected_window: int, duration_seconds: float) -> None:
        log(f"[boot] Focus shield active for HWND {protected_window} ({duration_seconds:.0f}s).")
        deadline = time.time() + duration_seconds
        while time.time() < deadline and not self.stop_event.is_set():
            foreground = get_foreground_window()
            if foreground and foreground != protected_window:
                foreground_pid = get_window_pid(foreground)
                process_name = ""
                executable = ""
                try:
                    process = psutil.Process(foreground_pid)
                    process_name = process.name().lower()
                    executable = (process.exe() or "").lower()
                except Exception:
                    pass
                runs_from_our_sandbox = False
                try:
                    sandbox_prefix = str(self.speedy.sandbox_dir.resolve()).lower()
                    runs_from_our_sandbox = bool(executable) and executable.startswith(sandbox_prefix)
                except Exception:
                    pass
                stealing_focus = runs_from_our_sandbox or any(
                    keyword in process_name for keyword in FOCUS_THIEF_KEYWORDS
                )
                if stealing_focus:
                    restore_and_focus(protected_window)
                else:
                    log("[boot] Focus shield: user switched apps; yielding.")
                    break
            time.sleep(FOCUS_SHIELD_POLL_INTERVAL_SECONDS)

    def _resolve_build(self, game_modules=None) -> Optional[SpeedyBuild]:
        if self._setting("dll_reroller_enabled", True):
            log("[boot] DLL re-roller enabled; requesting fresh camouflage build.")
            self.close_speedy_and_helpers(timeout=QUICK_KILL_TIMEOUT_SECONDS)
            fresh_build = self.speedy.prepare(game_modules=game_modules)
            if fresh_build:
                return fresh_build
            log("[boot] Re-roll failed; falling back to existing build.")
        return self.speedy.find_active_build()

    def _launch_speedy(self, build: Optional[SpeedyBuild]) -> bool:
        try:
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = SW_HIDE
        except Exception:
            startup_info = None

        if build and build.is_runnable():
            command = [str(build.speedy_exe)]
            working_directory = str(build.sandbox_dir)
            log(f"[boot] Launching camouflaged Speedy from temp sandbox '{working_directory}' "
                f"(exe '{build.speedy_exe}', injected DLL '{build.dll_64}').")
        else:
            command = ["powershell", "-NoProfile", "-Command", self._setting("speedy_command", "speedy")]
            working_directory = None
            log("[boot] Launching Speedy via fallback PowerShell command.")

        try:
            child = subprocess.Popen(
                command,
                creationflags=DETACHED_PROCESS,
                startupinfo=startup_info,
                cwd=working_directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            try:
                del child
            except Exception:
                pass
            return True
        except Exception as error:
            log_error(f"[boot] Failed to start Speedy: {error}")
            return False

    def run_speedy_workflow(self) -> None:
        try:
            self._run_speedy_workflow_impl()
        except Exception as error:
            log_error("Speedy workflow crashed", exc=error, where="boot.run_speedy_workflow")
            self.state.set(dll_status="Workflow error (see log)")
            try:
                self.close_speedy_and_helpers(timeout=QUICK_KILL_TIMEOUT_SECONDS)
            except Exception:
                pass

    def _wait_for_game_process(self, game_name: str) -> int:
        self.state.set(dll_status="Waiting for game process")
        deadline = time.time() + self._setting("game_launch_timeout_seconds", DEFAULT_LAUNCH_TIMEOUT_SECONDS)
        while time.time() < deadline:
            running = self.processes_named(game_name)
            if running:
                return running[0].pid
            if self.stop_event.is_set():
                return 0
            time.sleep(GENERIC_POLL_INTERVAL_SECONDS)
        return 0

    def _wait_for_loading_screen(self) -> float:
        base_wait = self._setting("speedy_delay_seconds", DEFAULT_LOADING_SCREEN_WAIT_SECONDS)
        jitter = random.uniform(0.0, self._setting("speedy_delay_jitter_seconds", DEFAULT_LOADING_WAIT_JITTER_SECONDS))
        total_wait = base_wait + jitter
        self.state.set(dll_status="Waiting for loader")
        log(f"[boot] Speedy workflow: game confirmed; waiting {total_wait:.2f}s "
            f"(base {base_wait}s + jitter {jitter:.2f}s) for loading screen.")
        time.sleep(total_wait)
        return total_wait

    def _run_speedy_workflow_impl(self) -> None:
        game_name = self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME)

        game_pid = self._wait_for_game_process(game_name)
        if not game_pid:
            log(f"[boot] Speedy workflow: {game_name} never appeared; skipping Speedy so the game is not crashed.")
            self.state.set(dll_status="Skipped (game not detected)")
            return

        self._wait_for_loading_screen()

        if not self.processes_named(game_name):
            log("[boot] Speedy workflow: game vanished during the wait; aborting Speedy.")
            self.state.set(dll_status="Aborted (game closed)")
            return

        loaded_paths = set()
        try:
            loaded_paths = self.loaded_dll_paths(game_pid)
        except Exception:
            pass
        game_modules = {Path(path).name for path in loaded_paths}

        already_injected, found_module = self.is_already_injected(game_pid)
        if already_injected:
            log(f"[boot] Speedy workflow: already injected (module '{found_module}' present); "
                f"skipping to avoid double injection.")
            self.state.set(dll_status=f"Already injected ({found_module})")
            return

        build = self._resolve_build(game_modules=game_modules)
        injected_dll = build.dll_64 if build else ORIGINAL_64

        if not self._inject_once(build, injected_dll):
            return
        self.speedy.mark_working(build)
        self.state.set(dll_status=f"Done ({injected_dll})")

        if self._setting("verify_injection_enabled", True):
            self._verify_and_maybe_reinject(game_pid, game_name, build, injected_dll)

    def _inject_once(self, build, injected_dll: str) -> bool:
        launcher_name = build.speedy_exe.name if build else DEFAULT_SPEEDY_PROCESS_NAME
        self.state.set(dll_status=f"Initializing ({injected_dll})")
        log(f"[boot] Speedy workflow: launching '{launcher_name}' silently (injected DLL '{injected_dll}').")
        if not self._launch_speedy(build):
            self.state.set(dll_status="Launch failed")
            return False

        dwell_seconds = self._setting("speedy_init_seconds", DEFAULT_SPEEDY_INIT_SECONDS) + random.random()
        log(f"[boot] Speedy workflow: dwelling a randomized {dwell_seconds:.4f}s while suppressing windows.")
        windows_to_hide = {launcher_name, DEFAULT_SPEEDY_PROCESS_NAME}
        if build:
            windows_to_hide.update({build.bridge_64, build.bridge_32})
        deadline = time.time() + dwell_seconds
        while time.time() < deadline:
            for window_name in windows_to_hide:
                self.hide_process_windows(window_name)
            time.sleep(WINDOW_SUPPRESSION_INTERVAL_SECONDS)

        log("[boot] Speedy workflow: terminating launcher + bridges after initialization.")
        try:
            self.close_windows_in_dir(self.speedy.sandbox_dir)
        except Exception as error:
            log(f"[boot] window-close (pre-terminate) error: {error}")
        self.close_speedy_and_helpers(timeout=5, build=build)
        return True

    def _verify_and_maybe_reinject(self, game_pid: int, game_name: str, build, injected_dll: str) -> None:
        verify_delay = self._setting("verify_injection_delay_seconds", DEFAULT_VERIFY_DELAY_SECONDS)
        log(f"[boot] Speedy workflow: verifying injection in {verify_delay:.0f}s...")
        time.sleep(verify_delay)

        if not self.processes_named(game_name):
            log("[boot] Verify: game no longer running; nothing to verify.")
            return

        if not self.loaded_dll_paths(game_pid):
            log("[boot] Verify: cannot read game modules (no admin?); skipping verify.")
            self.state.set(dll_status=f"Done ({injected_dll}, unverified)")
            return

        injected, found_module = self.is_already_injected(game_pid)
        if injected:
            log(f"[boot] Verify: injection confirmed (module '{found_module}' present).")
            self.state.set(dll_status=f"Injected & verified ({injected_dll})")
            return

        log("[boot] Verify: injection did NOT take; performing one automatic reinject.")
        self.state.set(dll_status=f"Reinjecting ({injected_dll})")
        if self._inject_once(build, injected_dll):
            self.speedy.mark_working(build)

        time.sleep(verify_delay)
        reinjected, reinjected_module = self.is_already_injected(game_pid)
        if reinjected:
            log(f"[boot] Verify: reinject succeeded (module '{reinjected_module}').")
            self.state.set(dll_status=f"Injected after reinject ({injected_dll})")
        else:
            log("[boot] Verify: reinject still not confirmed; leaving as-is.")
            self.state.set(dll_status=f"Reinject unconfirmed ({injected_dll})")

    def execute_launch_sequence(self) -> None:
        try:
            active_window = get_foreground_window()
            if active_window:
                threading.Thread(
                    target=self.focus_shield,
                    args=(active_window, self._setting("focus_shield_seconds", DEFAULT_FOCUS_SHIELD_SECONDS)),
                    daemon=True,
                ).start()
        except Exception as error:
            log(f"[boot] Could not start focus shield: {error}")

        log("[boot] Launch sequence: booting game.")
        self.launch_game()

        if self._setting("log_own_windows", True):
            diagnostic_timer = threading.Timer(OWN_WINDOW_DIAGNOSTIC_DELAY_SECONDS, self._log_own_windows)
            diagnostic_timer.daemon = True
            diagnostic_timer.start()

        if self._setting("speedy_enabled", True):
            threading.Thread(target=self.run_speedy_workflow, daemon=True).start()

    def _log_own_windows(self) -> None:
        try:
            own_windows = diagnose_own_windows(os.getpid())
            log(f"[boot] OWN-WINDOWS diagnostic: this process (pid {os.getpid()}) "
                f"owns {len(own_windows)} top-level window(s):")
            for window in own_windows:
                log(f"[boot]   - hwnd={window['hwnd']} taskbar={window['likely_taskbar']} "
                    f"visible={window['visible']} tool={window['toolwindow']} app={window['appwindow']} "
                    f"owned={window['has_owner']} class='{window['class']}' title='{window['title']}'")
            taskbar_windows = [window for window in own_windows if window["likely_taskbar"]]
            if taskbar_windows:
                log(f"[boot] OWN-WINDOWS: {len(taskbar_windows)} window(s) would show on the "
                    f"taskbar -> these are the stray entries.")
                if self._setting("strip_own_taskbar_windows", True):
                    for window in taskbar_windows:
                        if strip_from_taskbar(window["hwnd"]):
                            log(f"[boot]   stripped stray taskbar window hwnd={window['hwnd']} "
                                f"class='{window['class']}' title='{window['title']}'")
            else:
                log("[boot] OWN-WINDOWS: none of our windows should appear on the taskbar.")
        except Exception as error:
            log(f"[boot] OWN-WINDOWS diagnostic failed: {error}")

    def wait_for_game_open(self, before: Set[int],
                           timeout: Optional[int] = None,
                           process_name: Optional[str] = None) -> bool:
        timeout = timeout or self._setting("game_launch_timeout_seconds", DEFAULT_LAUNCH_TIMEOUT_SECONDS)
        process_name = (process_name
                        or self._setting("game_process_name", None)
                        or self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.processes_named(process_name):
                return True
            if self.stop_event.is_set():
                return False
            time.sleep(GENERIC_POLL_INTERVAL_SECONDS)
        return False

    def launch_game_flow(self) -> None:
        if not self._launch_lock.acquire(blocking=False):
            log("[boot] launch_game_flow: another launch is in progress; skipping.")
            self.clear_busy()
            return
        self.mark_busy(safety_clear_after=BUSY_LAUNCH_SAFETY_SECONDS)
        try:
            log("[boot] launch_game_flow: starting launch flow.")
            pids_before_launch = self.pid_snapshot()
            self.state.set(current_task="Launching game", game_state="Launching",
                           last_event="Launch requested")
            self.execute_launch_sequence()
            game_opened = self.wait_for_game_open(pids_before_launch)
            if game_opened:
                log("[boot] launch_game_flow: game open confirmed.")
                self.state.set(current_task="Monitoring TBH", game_state="Open",
                               last_event="Game appears open")
            else:
                log("[boot] launch_game_flow: could not confirm game open within timeout.")
                self.state.set(current_task="Monitoring TBH", game_state="Not confirmed",
                               last_event="Could not confirm game open")
        finally:
            self._launch_lock.release()
            grace_timer = threading.Timer(POST_LAUNCH_BUSY_GRACE_SECONDS, self.clear_busy)
            grace_timer.daemon = True
            grace_timer.start()

    def handle_hang(self) -> None:
        game_name = self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME)
        log(f"[boot] {game_name} hung; closing and relaunching.")
        self.state.set(current_task="Closing TBH", tbh_state="No, hung",
                       last_event=f"{game_name} stopped responding")
        if self.close_game():
            self.state.set(last_event=f"{game_name} closed")
        else:
            log(f"[boot] {game_name} may still be lingering.")
        self.launch_game_flow()

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        problems: list[str] = []

        try:
            running_count = len(self.processes_named(self._setting("task_process_name", DEFAULT_GAME_PROCESS_NAME)))
            report("boot.process_scan", "PASS", f"{running_count} TBH process(es) seen")
        except Exception as error:
            report("boot.process_scan", "FAIL", str(error))
            problems.append("process scan raised")

        try:
            snapshot = self.pid_snapshot()
            report("boot.pid_snapshot", "PASS" if snapshot else "INFO", f"{len(snapshot)} pids")
        except Exception as error:
            report("boot.pid_snapshot", "FAIL", str(error))
            problems.append("pid snapshot raised")

        try:
            report("boot.is_game_running", "PASS", str(self.is_game_running()))
        except Exception as error:
            report("boot.is_game_running", "FAIL", str(error))
            problems.append("is_game_running raised")

        try:
            report("boot.is_game_hung", "PASS", str(self.is_game_hung()))
        except Exception as error:
            report("boot.is_game_hung", "FAIL", str(error))
            problems.append("is_game_hung raised")

        try:
            build = self.speedy.find_active_build()
            report("boot.speedy_link", "PASS",
                   f"active build: {build.dll_64}" if build else "no active build yet")
        except Exception as error:
            report("boot.speedy_link", "FAIL", str(error))
            problems.append("speedy link raised")

        return problems
