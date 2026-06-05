from __future__ import annotations

import copy
import json
import os
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import stealth_temp
from watcher_config import log, log_error, load_config
from dll_mutate import mutate_dll, file_hash

ORIGINAL_64 = "speedpatch64.dll"
ORIGINAL_32 = "speedpatch32.dll"
SUPPORT_BINARIES = ["Speedy.exe", "bridge64.exe", "bridge32.exe", "config.ini"]
ORIGINAL_DLLS = [ORIGINAL_64, ORIGINAL_32]
ALL_SPEEDY_FILES = SUPPORT_BINARIES + ORIGINAL_DLLS
REQUIRED_PRISTINE_FILES = ["bridge64.exe", "bridge32.exe", ORIGINAL_64, ORIGINAL_32]

CAMOUFLAGE_NAME_LENGTH = len(ORIGINAL_64)
BLEND_WITH_GAME_PROBABILITY = 0.2
SANDBOX_META_FILENAME = "stealth_meta.json"
STORE_ROLE = "store"
SANDBOX_ROLE = "sandbox"
BACKUPS_DIRNAME = "backups"
PRISTINE_DIRNAME = "pristine"
BACKUP_META_FILENAME = "backup_meta.json"
STEM_TRAILING_CHARS = "0123456789_"

LAUNCHER_COVER_NAMES = [
    "SteamService.exe", "GameOverlay.exe", "nvcontainer.exe",
    "EpicWebHelper.exe", "crashpad_handler.exe", "UnityCrashHandler64.exe",
]
BRIDGE64_COVER_NAMES = [
    "overlay_host64.exe", "gpu_proc_64.exe", "audio_svc_64.exe",
    "render_host64.exe", "net_relay_64.exe",
]
BRIDGE32_COVER_NAMES = [
    "overlay_host32.exe", "gpu_proc_32.exe", "audio_svc_32.exe",
    "render_host32.exe", "net_relay_32.exe",
]

CAMOUFLAGE_NAMES: list[tuple[str, str]] = [
    ("steam_api_64.dll", "steam_api_32.dll"),
    ("discordgm_64.dll", "discordgm_32.dll"),
    ("qt5_corex_64.dll", "qt5_corex_32.dll"),
    ("sdl2_rtime64.dll", "sdl2_rtime32.dll"),
    ("directx_hk64.dll", "directx_hk32.dll"),
    ("overlay_hk64.dll", "overlay_hk32.dll"),
    ("bink2_vid_64.dll", "bink2_vid_32.dll"),
    ("physx_load64.dll", "physx_load32.dll"),
    ("steam_help64.dll", "steam_help32.dll"),
    ("unity_rndr64.dll", "unity_rndr32.dll"),
    ("fmod_audio64.dll", "fmod_audio32.dll"),
    ("unreal_hek64.dll", "unreal_hek32.dll"),
    ("game_confg64.dll", "game_confg32.dll"),
    ("crash_rprt64.dll", "crash_rprt32.dll"),
    ("gfx_extndr64.dll", "gfx_extndr32.dll"),
]


@dataclass
class SpeedyBuild:
    sandbox_dir: Path
    speedy_exe: Path
    bridge_64: str
    bridge_32: str
    dll_64: str
    dll_32: str
    original_dir: Path

    def is_runnable(self) -> bool:
        return self.speedy_exe.exists()


@dataclass
class RecoveryAction:
    kind: str
    entry: Optional[dict] = None


def locate_speedy_folder() -> Optional[Path]:
    candidate_folders: list[Path] = []
    try:
        resolved = shutil.which("Speedy") or shutil.which("Speedy.exe")
        if resolved:
            resolved_path = Path(resolved).resolve()
            if resolved_path.name.lower() == "speedy.exe":
                candidate_folders.append(resolved_path.parent)
    except Exception as error:
        log(f"[speedy] PATH resolver error: {error}")

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        winget_root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.exists():
            try:
                for folder in winget_root.iterdir():
                    if folder.is_dir() and "game1024.openspeedy" in folder.name.lower():
                        candidate_folders.append(folder)
            except Exception as error:
                log(f"[speedy] WinGet scan error: {error}")
        candidate_folders.append(Path(local_appdata) / "OpenSpeedy")

    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(env_var)
        if program_files:
            candidate_folders.append(Path(program_files) / "OpenSpeedy")

    for folder in candidate_folders:
        try:
            if folder.is_dir() and (folder / "Speedy.exe").exists():
                return folder
        except Exception:
            continue
    return None


def _name_stem(filename: str) -> str:
    return Path(filename).stem.lower().rstrip(STEM_TRAILING_CHARS)


def choose_camouflage_names(
    game_modules: Optional[set[str]] = None,
    avoid: Optional[str] = None,
) -> tuple[str, str]:
    selectable = list(CAMOUFLAGE_NAMES)

    if game_modules:
        loaded_lowercase = {module.lower() for module in game_modules}
        selectable = [(name_64, name_32) for (name_64, name_32) in selectable
                      if name_64.lower() not in loaded_lowercase and name_32.lower() not in loaded_lowercase]

    if avoid:
        without_last_used = [pair for pair in selectable if pair[0].lower() != avoid.lower()]
        if without_last_used:
            selectable = without_last_used

    if not selectable:
        selectable = [pair for pair in CAMOUFLAGE_NAMES
                      if pair[0].lower() != (avoid or "").lower()] or list(CAMOUFLAGE_NAMES)

    if game_modules and random.random() < BLEND_WITH_GAME_PROBABILITY:
        game_stems = {_name_stem(module) for module in {m.lower() for m in game_modules}}
        blended_options = [(name_64, name_32) for (name_64, name_32) in selectable
                           if _name_stem(name_64) in game_stems]
        if blended_options:
            chosen = random.choice(blended_options)
            log(f"[speedy] Camouflage name '{chosen[0]}' (blended with real modules).")
            return chosen

    chosen = random.choice(selectable)
    log(f"[speedy] Camouflage name '{chosen[0]}' (rotated; avoided '{avoid}').")
    return chosen


def _patch_bytes(data: bytes, replacements: list[tuple[str, str]]) -> tuple[bytes, bool]:
    patched = data
    changed = False
    for old_name, new_name in replacements:
        if len(old_name) != len(new_name):
            log(f"[speedy] Refusing unsafe rename '{old_name}' -> '{new_name}' (length differs).")
            continue
        for encoding in ("utf-8", "utf-16-le"):
            old_bytes = old_name.encode(encoding)
            new_bytes = new_name.encode(encoding)
            if old_bytes in patched:
                patched = patched.replace(old_bytes, new_bytes)
                changed = True
    return patched, changed


class SpeedyDLLManager:
    MAX_FAILURES = 3
    MAX_HISTORY = 5
    MAX_WORKING = 3

    def __init__(self) -> None:
        self.store_dir = self._find_or_create_store()
        self.backup_dir = self.store_dir / BACKUPS_DIRNAME
        self.pristine_dir = self.backup_dir / PRISTINE_DIRNAME
        self.meta_file = self.backup_dir / BACKUP_META_FILENAME

        self.sandbox_dir = stealth_temp.make_session_dir(role=SANDBOX_ROLE)
        self.sandbox_meta_file = self.sandbox_dir / SANDBOX_META_FILENAME

        self.active_64 = ORIGINAL_64
        self.active_32 = ORIGINAL_32
        self._diagnostics_dry_run = False

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.pristine_dir.mkdir(parents=True, exist_ok=True)
        self.meta = self._load_meta()
        log(f"[speedy] Store (backups/pristine) directory: '{self.store_dir}'.")
        log(f"[speedy] Session sandbox path: '{self.sandbox_dir}'.")

    def _find_or_create_store(self) -> Path:
        for folder, marker in stealth_temp.iter_owned_folders():
            if marker.get("role") != STORE_ROLE:
                continue
            if (folder / BACKUPS_DIRNAME / PRISTINE_DIRNAME).exists():
                log(f"[speedy] Reusing existing temp store: '{folder}'.")
                return folder
        return stealth_temp.make_session_dir(role=STORE_ROLE)

    def _default_meta(self) -> dict:
        return {"last_working_64_dll": None, "last_dll_64": None,
                "failure_count": 0, "history": []}

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                with open(self.meta_file, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception as error:
                log(f"[speedy] Failed to read backup meta, resetting: {error}")
        meta = self._default_meta()
        self._write_meta(meta)
        return meta

    def _write_meta(self, meta: Optional[dict] = None) -> None:
        if self._diagnostics_dry_run:
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            with open(self.meta_file, "w", encoding="utf-8") as handle:
                json.dump(meta if meta is not None else self.meta, handle, indent=4)
        except Exception as error:
            log(f"[speedy] Failed to write backup meta: {error}")

    def ensure_pristine_baseline(self, original_dir: Path) -> bool:
        copied_count = 0
        for filename in ALL_SPEEDY_FILES:
            destination = self.pristine_dir / filename
            if destination.exists():
                continue
            source = original_dir / filename
            if not source.exists():
                if filename in ORIGINAL_DLLS or filename in ("Speedy.exe", "bridge64.exe", "bridge32.exe"):
                    log(f"[speedy] Pristine source missing required file: {filename}")
                continue
            try:
                shutil.copy2(source, destination)
                copied_count += 1
            except Exception as error:
                log(f"[speedy] Failed to copy pristine {filename}: {error}")
                return False
        missing = [name for name in REQUIRED_PRISTINE_FILES if not (self.pristine_dir / name).exists()]
        if missing:
            log(f"[speedy] Pristine baseline incomplete, missing: {missing}")
            return False
        if copied_count:
            log(f"[speedy] Established pristine baseline in temp ({copied_count} file(s)).")
        return True

    def prepare(self, force_names: Optional[tuple[str, str]] = None,
                game_modules: Optional[set[str]] = None) -> Optional[SpeedyBuild]:
        original_dir = locate_speedy_folder()
        if not original_dir:
            log("[speedy] prepare: could not locate OpenSpeedy install.")
            return None
        log(f"[speedy] prepare: located install at '{original_dir}'.")

        if not self.ensure_pristine_baseline(original_dir):
            log("[speedy] prepare: pristine baseline unavailable; aborting.")
            return None

        if force_names:
            new_64, new_32 = force_names
        else:
            last_used = self.meta.get("last_dll_64") or self.active_64
            new_64, new_32 = choose_camouflage_names(game_modules, avoid=last_used)
        self.active_64, self.active_32 = new_64, new_32
        self.meta["last_dll_64"] = new_64
        self._write_meta()

        launcher_name = random.choice(LAUNCHER_COVER_NAMES)
        bridge_64_name = random.choice(BRIDGE64_COVER_NAMES)
        bridge_32_name = random.choice(BRIDGE32_COVER_NAMES)

        try:
            self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            log_error(f"[speedy] prepare: cannot create sandbox {self.sandbox_dir}: {error}")
            return None

        log(f"[speedy] prepare: rolling camouflage DLL '{new_64}' / '{new_32}'.")
        log(f"[speedy] prepare: injector cover names launcher='{launcher_name}', "
            f"bridges='{bridge_64_name}'/'{bridge_32_name}'.")
        log(f"[speedy] prepare: sandbox directory is '{self.sandbox_dir}'.")

        self._wipe_sandbox_keeping_marker()

        launcher_destination = self.sandbox_dir / launcher_name
        try:
            shutil.copy2(self.pristine_dir / "Speedy.exe", launcher_destination)
            if (self.pristine_dir / "config.ini").exists():
                shutil.copy2(self.pristine_dir / "config.ini", self.sandbox_dir / "config.ini")
        except Exception as error:
            log_error(f"[speedy] prepare: failed copying launcher/config: {error}")
            return None

        dll_renames = [(ORIGINAL_64, new_64), (ORIGINAL_32, new_32)]
        bridge_renames = [("bridge64.exe", bridge_64_name), ("bridge32.exe", bridge_32_name)]
        for original_bridge, cover_name in bridge_renames:
            source = self.pristine_dir / original_bridge
            if not source.exists():
                continue
            try:
                bridge_bytes = source.read_bytes()
            except Exception as error:
                log(f"[speedy] prepare: cannot read pristine {original_bridge}: {error}")
                return None
            patched_bytes, changed = _patch_bytes(bridge_bytes, dll_renames)
            try:
                (self.sandbox_dir / cover_name).write_bytes(patched_bytes)
            except Exception as error:
                log_error(f"[speedy] prepare: failed writing {cover_name}: {error}")
                return None
            log(f"[speedy] prepare: {original_bridge} -> {cover_name} "
                f"{'patched' if changed else '(no name match)'}.")

        use_pefile_mutation = bool(load_config().get("dll_mutation_pefile_enabled", False))
        for original_dll, new_name in dll_renames:
            source = self.pristine_dir / original_dll
            if not source.exists():
                continue
            destination = self.sandbox_dir / new_name
            try:
                shutil.copy2(source, destination)
            except Exception as error:
                log_error(f"[speedy] prepare: failed generating {new_name}: {error}")
                return None
            log(f"[speedy] prepare: mutating '{new_name}' (pefile={use_pefile_mutation})...")
            mutate_dll(destination, use_pefile=use_pefile_mutation)
            log(f"[speedy] prepare: generated + mutated camouflage DLL '{new_name}' "
                f"(sha256 {file_hash(destination)[:12]}...).")

        self._write_sandbox_meta(new_64, new_32, launcher_name, bridge_64_name, bridge_32_name, original_dir)
        self._record_history(new_64, new_32, launcher_name, bridge_64_name, bridge_32_name)

        build = SpeedyBuild(
            sandbox_dir=self.sandbox_dir,
            speedy_exe=launcher_destination,
            bridge_64=bridge_64_name,
            bridge_32=bridge_32_name,
            dll_64=new_64,
            dll_32=new_32,
            original_dir=original_dir,
        )
        if not build.is_runnable():
            log("[speedy] prepare: launcher missing from sandbox after build.")
            return None
        log(f"[speedy] prepare: build ready -> launcher '{build.speedy_exe}', "
            f"injected DLL '{new_64}' (32-bit '{new_32}'), sandbox '{self.sandbox_dir}'.")
        return build

    def _wipe_sandbox_keeping_marker(self) -> None:
        for item in self.sandbox_dir.iterdir():
            if item.name == stealth_temp.MARKER_NAME:
                continue
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except Exception as error:
                log(f"[speedy] prepare: could not remove old {item.name}: {error}")

    def _write_sandbox_meta(self, dll_64, dll_32, launcher, bridge_64, bridge_32, original_dir) -> None:
        sandbox_metadata = {
            "patched": True,
            "active_64_dll": dll_64,
            "active_32_dll": dll_32,
            "launcher": launcher,
            "bridge_64": bridge_64,
            "bridge_32": bridge_32,
            "original_dir": str(original_dir),
            "sandbox": str(self.sandbox_dir),
        }
        try:
            with open(self.sandbox_meta_file, "w", encoding="utf-8") as handle:
                json.dump(sandbox_metadata, handle, indent=4)
        except Exception as error:
            log(f"[speedy] Failed writing sandbox meta: {error}")

    def find_active_build(self) -> Optional[SpeedyBuild]:
        own_meta = self.sandbox_dir / SANDBOX_META_FILENAME
        chosen_meta: Optional[Path] = own_meta if own_meta.exists() else None
        if chosen_meta is None:
            newest_mtime = 0.0
            for folder, _marker in stealth_temp.iter_owned_folders():
                candidate_meta = folder / SANDBOX_META_FILENAME
                if candidate_meta.exists():
                    try:
                        mtime = candidate_meta.stat().st_mtime
                        if mtime > newest_mtime:
                            newest_mtime = mtime
                            chosen_meta = candidate_meta
                    except Exception:
                        pass
        if not chosen_meta:
            return None
        try:
            with open(chosen_meta, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except Exception as error:
            log(f"[speedy] find_active_build read error: {error}")
            return None

        sandbox = chosen_meta.parent
        launcher = sandbox / metadata.get("launcher", "Speedy.exe")
        if not launcher.exists():
            return None
        return SpeedyBuild(
            sandbox_dir=sandbox,
            speedy_exe=launcher,
            bridge_64=metadata.get("bridge_64", "bridge64.exe"),
            bridge_32=metadata.get("bridge_32", "bridge32.exe"),
            dll_64=metadata.get("active_64_dll", ORIGINAL_64),
            dll_32=metadata.get("active_32_dll", ORIGINAL_32),
            original_dir=Path(metadata.get("original_dir", "")),
        )

    def _record_history(self, dll_64, dll_32, launcher, bridge_64, bridge_32) -> str:
        folder_timestamp = time.strftime("%Y%m%d_%H%M%S")
        state_name = f"state_{folder_timestamp}_{dll_64[:-4]}"
        state_dir = self.backup_dir / state_name
        state_dir.mkdir(parents=True, exist_ok=True)
        for item in self.sandbox_dir.iterdir():
            if item.is_file() and item.name != stealth_temp.MARKER_NAME:
                try:
                    shutil.copy2(item, state_dir / item.name)
                except Exception:
                    pass
        self.meta["history"].append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "state_dir": state_name,
            "dll_64": dll_64, "dll_32": dll_32,
            "launcher": launcher, "bridge_64": bridge_64, "bridge_32": bridge_32,
            "status": "untested",
        })
        self._trim_history()
        self._write_meta()
        return state_name

    def _trim_history(self) -> None:
        history = self.meta["history"]
        if len(history) <= self.MAX_HISTORY:
            return
        log("[speedy] Trimming backup history (> limit).")
        working_entries = [entry for entry in history if entry["status"] == "working"]
        other_entries = [entry for entry in history if entry["status"] != "working"]
        if len(working_entries) > self.MAX_WORKING:
            for stale in working_entries[:-self.MAX_WORKING]:
                self._delete_state(stale["state_dir"])
            working_entries = working_entries[-self.MAX_WORKING:]
        allowed_others = max(0, self.MAX_HISTORY - len(working_entries))
        if len(other_entries) > allowed_others:
            for stale in (other_entries[:-allowed_others] if allowed_others else other_entries):
                self._delete_state(stale["state_dir"])
            other_entries = other_entries[-allowed_others:] if allowed_others else []
        self.meta["history"] = sorted(working_entries + other_entries, key=lambda entry: entry["timestamp"])

    def _delete_state(self, state_name: str) -> None:
        if self._diagnostics_dry_run:
            return
        state_dir = self.backup_dir / state_name
        if state_dir.exists():
            try:
                shutil.rmtree(state_dir)
                log(f"[speedy] Pruned state folder '{state_name}'.")
            except Exception:
                pass

    def mark_working(self, build: Optional[SpeedyBuild] = None) -> None:
        if not self.meta["history"]:
            return
        active_entry = self.meta["history"][-1]
        active_entry["status"] = "working"
        self.meta["last_working_64_dll"] = active_entry["dll_64"]
        self.meta["failure_count"] = 0
        self._write_meta()
        log(f"[speedy] Build '{active_entry['dll_64']}' validated WORKING; failures reset.")

    def handle_failure(self, build: Optional[SpeedyBuild] = None) -> RecoveryAction:
        if not self.meta["history"]:
            return RecoveryAction("none")
        active_entry = self.meta["history"][-1]
        active_entry["status"] = "faulty"
        self.meta["failure_count"] += 1
        failure_count = self.meta["failure_count"]
        self._write_meta()
        log(f"[speedy] Build '{active_entry['dll_64']}' marked FAULTY ({failure_count}/{self.MAX_FAILURES}).")

        if failure_count < self.MAX_FAILURES:
            rollback_target = self._find_previous_valid()
            if rollback_target:
                log(f"[speedy] Recovery: rolling back to '{rollback_target['state_dir']}'.")
                return RecoveryAction("rollback", rollback_target)
            return RecoveryAction("none")

        last_working = self._find_latest_working()
        if last_working:
            log(f"[speedy] Recovery: restoring last working state from {last_working['timestamp']}.")
            self._prune_after(last_working["timestamp"])
            self.meta["failure_count"] = 0
            self._write_meta()
            return RecoveryAction("restore_working", last_working)

        log("[speedy] Recovery: no working state; falling back to pristine.")
        self.meta["failure_count"] = 0
        self._write_meta()
        return RecoveryAction("pristine")

    def _find_previous_valid(self) -> Optional[dict]:
        for entry in reversed(self.meta["history"][:-1]):
            if entry["status"] != "faulty":
                return entry
        return None

    def _find_latest_working(self) -> Optional[dict]:
        for entry in reversed(self.meta["history"]):
            if entry["status"] == "working":
                return entry
        return None

    def _prune_after(self, timestamp: str) -> None:
        kept_entries = []
        for entry in self.meta["history"]:
            if entry["timestamp"] <= timestamp:
                kept_entries.append(entry)
            else:
                self._delete_state(entry["state_dir"])
        self.meta["history"] = kept_entries

    def apply_recovery(self, action: RecoveryAction) -> Optional[SpeedyBuild]:
        if action.kind in ("rollback", "restore_working") and action.entry:
            return self._restore_state_to_sandbox(action.entry)
        if action.kind == "pristine":
            return self._restore_pristine_to_sandbox()
        return None

    def _clear_sandbox(self) -> None:
        if not self.sandbox_dir.exists():
            self.sandbox_dir = stealth_temp.make_session_dir(role=SANDBOX_ROLE)
            self.sandbox_meta_file = self.sandbox_dir / SANDBOX_META_FILENAME
            return
        for item in self.sandbox_dir.iterdir():
            if item.name == stealth_temp.MARKER_NAME:
                continue
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except Exception:
                pass

    def _restore_state_to_sandbox(self, entry: dict) -> Optional[SpeedyBuild]:
        state_dir = self.backup_dir / entry["state_dir"]
        if not state_dir.exists():
            log(f"[speedy] Restore failed: state '{entry['state_dir']}' missing.")
            return None
        self._clear_sandbox()
        for item in state_dir.iterdir():
            if item.is_file():
                try:
                    shutil.copy2(item, self.sandbox_dir / item.name)
                except Exception:
                    pass
        self.active_64, self.active_32 = entry["dll_64"], entry["dll_32"]
        self._write_sandbox_meta(entry["dll_64"], entry["dll_32"],
                                 entry.get("launcher", "Speedy.exe"),
                                 entry.get("bridge_64", "bridge64.exe"),
                                 entry.get("bridge_32", "bridge32.exe"),
                                 locate_speedy_folder() or self.store_dir)
        log(f"[speedy] Restored state '{entry['state_dir']}' to sandbox.")
        return self.find_active_build()

    def _restore_pristine_to_sandbox(self) -> Optional[SpeedyBuild]:
        self._clear_sandbox()
        for item in self.pristine_dir.iterdir():
            if item.is_file():
                try:
                    shutil.copy2(item, self.sandbox_dir / item.name)
                except Exception:
                    pass
        self.active_64, self.active_32 = ORIGINAL_64, ORIGINAL_32
        self._write_sandbox_meta(ORIGINAL_64, ORIGINAL_32, "Speedy.exe",
                                 "bridge64.exe", "bridge32.exe",
                                 locate_speedy_folder() or self.store_dir)
        log("[speedy] Restored pristine (unpatched) files to sandbox.")
        return self.find_active_build()

    def rollback_all(self) -> None:
        for folder, marker in stealth_temp.iter_owned_folders():
            if marker.get("role") == SANDBOX_ROLE:
                stealth_temp.remove_dir(folder)
        if self.backup_dir.exists():
            for item in self.backup_dir.iterdir():
                if item == self.pristine_dir:
                    continue
                try:
                    item.unlink() if item.is_file() else shutil.rmtree(item)
                except Exception:
                    pass
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.pristine_dir.mkdir(parents=True, exist_ok=True)
        self.meta = self._default_meta()
        self._write_meta()
        self.sandbox_dir = stealth_temp.make_session_dir(role=SANDBOX_ROLE)
        self.sandbox_meta_file = self.sandbox_dir / SANDBOX_META_FILENAME
        log("[speedy] ROLLBACK: cleared all sandboxes + states; pristine baseline kept.")

    def unpatch(self) -> None:
        for folder, marker in stealth_temp.iter_owned_folders():
            if marker.get("role") in (SANDBOX_ROLE, STORE_ROLE):
                stealth_temp.remove_dir(folder)
        self.meta = self._default_meta()
        self.store_dir = stealth_temp.make_session_dir(role=STORE_ROLE)
        self.backup_dir = self.store_dir / BACKUPS_DIRNAME
        self.pristine_dir = self.backup_dir / PRISTINE_DIRNAME
        self.meta_file = self.backup_dir / BACKUP_META_FILENAME
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.pristine_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_dir = stealth_temp.make_session_dir(role=SANDBOX_ROLE)
        self.sandbox_meta_file = self.sandbox_dir / SANDBOX_META_FILENAME
        self._write_meta()
        log("[speedy] UNPATCH: temp store (incl. pristine) + sandboxes deleted; zero footprint.")

    def cleanup_session(self) -> None:
        stealth_temp.remove_dir(self.sandbox_dir)

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        problems: list[str] = []

        original_dir = locate_speedy_folder()
        if original_dir and original_dir.exists():
            report("speedy.locate", "PASS", str(original_dir))
        else:
            report("speedy.locate", "FAIL", "OpenSpeedy install not found")
            problems.append("speedy install not found")

        report("speedy.store_in_temp", "PASS", f"store at {self.store_dir}")
        report("speedy.sandbox_in_temp", "PASS", f"sandbox at {self.sandbox_dir}")

        try:
            probe_file = self.sandbox_dir / "._probe.tmp"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            report("speedy.sandbox_write", "PASS", "writable")
        except Exception as error:
            report("speedy.sandbox_write", "FAIL", str(error))
            problems.append("sandbox not writable")

        if original_dir:
            try:
                baseline_ok = self.ensure_pristine_baseline(original_dir)
                report("speedy.pristine", "PASS" if baseline_ok else "FAIL",
                       "baseline ready in temp" if baseline_ok else "baseline incomplete")
                if not baseline_ok:
                    problems.append("pristine baseline incomplete")
            except Exception as error:
                report("speedy.pristine", "FAIL", str(error))
                problems.append("pristine baseline raised")
        else:
            report("speedy.pristine", "INFO", "skipped (no install)")

        try:
            patched, changed = _patch_bytes(b"load speedpatch64.dll now",
                                            [(ORIGINAL_64, "steam_api_64.dll")])
            byte_patch_ok = changed and b"steam_api_64.dll" in patched
            report("speedy.byte_patch", "PASS" if byte_patch_ok else "FAIL",
                   "ASCII rename works" if byte_patch_ok else "patch produced no change")
            if not byte_patch_ok:
                problems.append("byte patch broken")
        except Exception as error:
            report("speedy.byte_patch", "FAIL", str(error))
            problems.append("byte patch raised")

        try:
            chosen_64, _chosen_32 = choose_camouflage_names({"steam_api64.dll", "unity_player.dll"})
            report("speedy.name_select", "PASS", f"chose {chosen_64}")
        except Exception as error:
            report("speedy.name_select", "FAIL", str(error))
            problems.append("name select raised")

        try:
            saved_meta = copy.deepcopy(self.meta)
            self._diagnostics_dry_run = True
            try:
                probe_entry = {"timestamp": "9999-99-99", "state_dir": "_diag_probe_",
                               "dll_64": ORIGINAL_64, "dll_32": ORIGINAL_32, "status": "untested"}
                self.meta.setdefault("history", []).append(probe_entry)
                action = self.handle_failure()
                report("speedy.recovery", "PASS", f"failure path -> {action.kind}")
            finally:
                self._diagnostics_dry_run = False
                self.meta = saved_meta
        except Exception as error:
            report("speedy.recovery", "FAIL", str(error))
            problems.append("recovery path raised")

        return problems


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    return SpeedyDLLManager().diagnose(report)


if __name__ == "__main__":
    def _print_row(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    found = diagnose(_print_row)
    print("OK" if not found else f"ISSUES: {found}")
