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
from watcher_config import logInfo, logError, loadConfig
from dll_mutate import mutateDll, fileHash


speedyBinaries = ["Speedy.exe", "bridge64.exe", "bridge32.exe", "config.ini"]
speedyDlls = ["speedpatch64.dll", "speedpatch32.dll"]
allSpeedyFiles = speedyBinaries + speedyDlls

original64 = "speedpatch64.dll"
original32 = "speedpatch32.dll"

launcherCoverNames = [
    "SteamService.exe", "GameOverlay.exe", "nvcontainer.exe",
    "EpicWebHelper.exe", "crashpad_handler.exe", "UnityCrashHandler64.exe",
]
bridge64CoverNames = [
    "overlay_host64.exe", "gpu_proc_64.exe", "audio_svc_64.exe",
    "render_host64.exe", "net_relay_64.exe",
]
bridge32CoverNames = [
    "overlay_host32.exe", "gpu_proc_32.exe", "audio_svc_32.exe",
    "render_host32.exe", "net_relay_32.exe",
]

camouflageNames = [
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
    sandboxDir: Path
    speedyExe: Path
    bridge64: str
    bridge32: str
    dll64: str
    dll32: str
    originalDir: Path

    def isRunnable(self) -> bool:
        return self.speedyExe.exists()


@dataclass
class RecoveryAction:
    kind: str
    entry: Optional[dict] = None


def locateSpeedyFolder() -> Optional[Path]:
    candidates: list = []
    try:
        resolved = shutil.which("Speedy") or shutil.which("Speedy.exe")
        if resolved:
            path = Path(resolved).resolve()
            if path.name.lower() == "speedy.exe":
                candidates.append(path.parent)
    except Exception as error:
        logInfo(f"[speedy] PATH resolver error: {error}")

    localAppData = os.environ.get("LOCALAPPDATA")
    if localAppData:
        wingetRoot = Path(localAppData) / "Microsoft" / "WinGet" / "Packages"
        if wingetRoot.exists():
            try:
                for folder in wingetRoot.iterdir():
                    if folder.is_dir() and "game1024.openspeedy" in folder.name.lower():
                        candidates.append(folder)
            except Exception as error:
                logInfo(f"[speedy] WinGet scan error: {error}")
        candidates.append(Path(localAppData) / "OpenSpeedy")

    for variableName in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variableName)
        if root:
            candidates.append(Path(root) / "OpenSpeedy")

    for folder in candidates:
        try:
            if folder.is_dir() and (folder / "Speedy.exe").exists():
                return folder
        except Exception:
            continue
    return None


def chooseCamouflageNames(gameModules: Optional[set] = None,
                          avoid: Optional[str] = None) -> tuple:
    pool = list(camouflageNames)

    if gameModules:
        loweredModules = {name.lower() for name in gameModules}
        pool = [(name64, name32) for (name64, name32) in pool
                if name64.lower() not in loweredModules and name32.lower() not in loweredModules]

    if avoid:
        withoutAvoid = [pair for pair in pool if pair[0].lower() != avoid.lower()]
        if withoutAvoid:
            pool = withoutAvoid

    if not pool:
        pool = [pair for pair in camouflageNames if pair[0].lower() != (avoid or "").lower()] \
               or list(camouflageNames)

    if gameModules and random.random() < 0.2:
        gameStems = {Path(name).stem.lower().rstrip("0123456789_")
                     for name in {raw.lower() for raw in gameModules}}
        blended = [(name64, name32) for (name64, name32) in pool
                   if Path(name64).stem.lower().rstrip("0123456789_") in gameStems]
        if blended:
            choice = random.choice(blended)
            logInfo(f"[speedy] Camouflage name '{choice[0]}' (blended with real modules).")
            return choice

    choice = random.choice(pool)
    logInfo(f"[speedy] Camouflage name '{choice[0]}' (rotated; avoided '{avoid}').")
    return choice


def patchBytes(data: bytes, replacements: list) -> tuple:
    output = data
    changed = False
    for oldName, newName in replacements:
        if len(oldName) != len(newName):
            logInfo(f"[speedy] Refusing unsafe rename '{oldName}' -> '{newName}' (length differs).")
            continue
        for encoding in ("utf-8", "utf-16-le"):
            oldBytes = oldName.encode(encoding)
            newBytes = newName.encode(encoding)
            if oldBytes in output:
                output = output.replace(oldBytes, newBytes)
                changed = True
    return output, changed


class SpeedyDLLManager:
    maxFailures = 3
    maxHistory = 5
    maxWorking = 3

    def __init__(self) -> None:
        self.diagMode = False
        self.storeDir = self.ensureStoreDir()
        self.backupDir = self.storeDir / "backups"
        self.pristineDir = self.backupDir / "pristine"
        self.metaFile = self.backupDir / "backup_meta.json"

        self.sandboxDir = stealth_temp.makeSessionDir(role="sandbox")
        self.sandboxMetaFile = self.sandboxDir / "stealth_meta.json"

        self.active64 = original64
        self.active32 = original32

        self.backupDir.mkdir(parents=True, exist_ok=True)
        self.pristineDir.mkdir(parents=True, exist_ok=True)
        self.meta = self.loadMeta()
        logInfo(f"[speedy] Store (backups/pristine) directory: '{self.storeDir}'.")
        logInfo(f"[speedy] Session sandbox path: '{self.sandboxDir}'.")

    def ensureStoreDir(self) -> Path:
        for folder, data in stealth_temp.iterOwnedFolders():
            if data.get("role") != "store":
                continue
            if (folder / "backups" / "pristine").exists():
                logInfo(f"[speedy] Reusing existing temp store: '{folder}'.")
                return folder
        return stealth_temp.makeSessionDir(role="store")

    def defaultMeta(self) -> dict:
        return {"last_working_64_dll": None, "last_dll_64": None,
                "failure_count": 0, "history": []}

    def loadMeta(self) -> dict:
        if self.metaFile.exists():
            try:
                with open(self.metaFile, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except Exception as error:
                logInfo(f"[speedy] Failed to read backup meta, resetting: {error}")
        meta = self.defaultMeta()
        self.writeMeta(meta)
        return meta

    def writeMeta(self, meta: Optional[dict] = None) -> None:
        if self.diagMode:
            return
        try:
            self.backupDir.mkdir(parents=True, exist_ok=True)
            with open(self.metaFile, "w", encoding="utf-8") as handle:
                json.dump(meta if meta is not None else self.meta, handle, indent=4)
        except Exception as error:
            logInfo(f"[speedy] Failed to write backup meta: {error}")

    def ensurePristineBaseline(self, originalDir: Path) -> bool:
        copied = 0
        for name in allSpeedyFiles:
            destination = self.pristineDir / name
            if destination.exists():
                continue
            source = originalDir / name
            if not source.exists():
                if name in speedyDlls or name in ("Speedy.exe", "bridge64.exe", "bridge32.exe"):
                    logInfo(f"[speedy] Pristine source missing required file: {name}")
                continue
            try:
                shutil.copy2(source, destination)
                copied += 1
            except Exception as error:
                logInfo(f"[speedy] Failed to copy pristine {name}: {error}")
                return False
        required = ["bridge64.exe", "bridge32.exe", original64, original32]
        missing = [name for name in required if not (self.pristineDir / name).exists()]
        if missing:
            logInfo(f"[speedy] Pristine baseline incomplete, missing: {missing}")
            return False
        if copied:
            logInfo(f"[speedy] Established pristine baseline in temp ({copied} file(s)).")
        return True

    def prepare(self, forceNames: Optional[tuple] = None,
                gameModules: Optional[set] = None) -> Optional[SpeedyBuild]:
        originalDir = locateSpeedyFolder()
        if not originalDir:
            logInfo("[speedy] prepare: could not locate OpenSpeedy install.")
            return None
        logInfo(f"[speedy] prepare: located install at '{originalDir}'.")

        if not self.ensurePristineBaseline(originalDir):
            logInfo("[speedy] prepare: pristine baseline unavailable; aborting.")
            return None

        if forceNames:
            new64, new32 = forceNames
        else:
            avoid = self.meta.get("last_dll_64") or self.active64
            new64, new32 = chooseCamouflageNames(gameModules, avoid=avoid)
        self.active64, self.active32 = new64, new32
        self.meta["last_dll_64"] = new64
        self.writeMeta()

        launcherName = random.choice(launcherCoverNames)
        bridge64Name = random.choice(bridge64CoverNames)
        bridge32Name = random.choice(bridge32CoverNames)

        try:
            self.sandboxDir.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            logError(f"[speedy] prepare: cannot create sandbox {self.sandboxDir}", error)
            return None

        logInfo(f"[speedy] prepare: rolling camouflage DLL '{new64}' / '{new32}'.")
        logInfo(f"[speedy] prepare: injector cover names launcher='{launcherName}', "
                f"bridges='{bridge64Name}'/'{bridge32Name}'.")
        logInfo(f"[speedy] prepare: sandbox directory is '{self.sandboxDir}'.")

        for item in self.sandboxDir.iterdir():
            if item.name == stealth_temp.markerName:
                continue
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except Exception as error:
                logInfo(f"[speedy] prepare: could not remove old {item.name}: {error}")

        launcherDestination = self.sandboxDir / launcherName
        try:
            shutil.copy2(self.pristineDir / "Speedy.exe", launcherDestination)
            if (self.pristineDir / "config.ini").exists():
                shutil.copy2(self.pristineDir / "config.ini", self.sandboxDir / "config.ini")
        except Exception as error:
            logError("[speedy] prepare: failed copying launcher/config", error)
            return None

        replacements = [(original64, new64), (original32, new32)]
        bridgeTargets = [("bridge64.exe", bridge64Name), ("bridge32.exe", bridge32Name)]
        for originalBridge, coverName in bridgeTargets:
            source = self.pristineDir / originalBridge
            if not source.exists():
                continue
            try:
                data = source.read_bytes()
            except Exception as error:
                logInfo(f"[speedy] prepare: cannot read pristine {originalBridge}: {error}")
                return None
            patched, changed = patchBytes(data, replacements)
            try:
                (self.sandboxDir / coverName).write_bytes(patched)
            except Exception as error:
                logError(f"[speedy] prepare: failed writing {coverName}", error)
                return None
            logInfo(f"[speedy] prepare: {originalBridge} -> {coverName} "
                    f"{'patched' if changed else '(no name match)'}.")

        usePefileMutation = bool(loadConfig().get("dll_mutation_pefile_enabled", False))
        for originalDll, newName in ((original64, new64), (original32, new32)):
            source = self.pristineDir / originalDll
            if not source.exists():
                continue
            destination = self.sandboxDir / newName
            try:
                shutil.copy2(source, destination)
            except Exception as error:
                logError(f"[speedy] prepare: failed generating {newName}", error)
                return None
            logInfo(f"[speedy] prepare: mutating '{newName}' (pefile={usePefileMutation})...")
            mutateDll(destination, usePefile=usePefileMutation)
            logInfo(f"[speedy] prepare: generated + mutated camouflage DLL '{newName}' "
                    f"(sha256 {fileHash(destination)[:12]}...).")

        self.writeSandboxMeta(new64, new32, launcherName, bridge64Name, bridge32Name, originalDir)
        self.recordHistory(new64, new32, launcherName, bridge64Name, bridge32Name)

        build = SpeedyBuild(
            sandboxDir=self.sandboxDir,
            speedyExe=launcherDestination,
            bridge64=bridge64Name,
            bridge32=bridge32Name,
            dll64=new64,
            dll32=new32,
            originalDir=originalDir,
        )
        if not build.isRunnable():
            logInfo("[speedy] prepare: launcher missing from sandbox after build.")
            return None
        logInfo(f"[speedy] prepare: build ready -> launcher '{build.speedyExe}', "
                f"injected DLL '{new64}' (32-bit '{new32}'), sandbox '{self.sandboxDir}'.")
        return build

    def writeSandboxMeta(self, dll64, dll32, launcher, bridge64, bridge32, originalDir) -> None:
        payload = {
            "patched": True,
            "active_64_dll": dll64,
            "active_32_dll": dll32,
            "launcher": launcher,
            "bridge_64": bridge64,
            "bridge_32": bridge32,
            "original_dir": str(originalDir),
            "sandbox": str(self.sandboxDir),
        }
        try:
            with open(self.sandboxMetaFile, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=4)
        except Exception as error:
            logInfo(f"[speedy] Failed writing sandbox meta: {error}")

    def findActiveBuild(self) -> Optional[SpeedyBuild]:
        ownMeta = self.sandboxDir / "stealth_meta.json"
        newest = ownMeta if ownMeta.exists() else None
        if newest is None:
            newestModified = 0.0
            for folder, data in stealth_temp.iterOwnedFolders():
                meta = folder / "stealth_meta.json"
                if meta.exists():
                    try:
                        modified = meta.stat().st_mtime
                        if modified > newestModified:
                            newestModified = modified
                            newest = meta
                    except Exception:
                        pass
        if not newest:
            return None
        try:
            with open(newest, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as error:
            logInfo(f"[speedy] findActiveBuild read error: {error}")
            return None

        sandbox = newest.parent
        launcher = sandbox / data.get("launcher", "Speedy.exe")
        if not launcher.exists():
            return None
        return SpeedyBuild(
            sandboxDir=sandbox,
            speedyExe=launcher,
            bridge64=data.get("bridge_64", "bridge64.exe"),
            bridge32=data.get("bridge_32", "bridge32.exe"),
            dll64=data.get("active_64_dll", original64),
            dll32=data.get("active_32_dll", original32),
            originalDir=Path(data.get("original_dir", "")),
        )

    def recordHistory(self, dll64, dll32, launcher, bridge64, bridge32) -> str:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        stateName = f"state_{timestamp}_{dll64[:-4]}"
        stateDir = self.backupDir / stateName
        stateDir.mkdir(parents=True, exist_ok=True)
        for item in self.sandboxDir.iterdir():
            if item.is_file() and item.name != stealth_temp.markerName:
                try:
                    shutil.copy2(item, stateDir / item.name)
                except Exception:
                    pass
        self.meta["history"].append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "state_dir": stateName,
            "dll_64": dll64, "dll_32": dll32,
            "launcher": launcher, "bridge_64": bridge64, "bridge_32": bridge32,
            "status": "untested",
        })
        self.trimHistory()
        self.writeMeta()
        return stateName

    def trimHistory(self) -> None:
        history = self.meta["history"]
        if len(history) <= self.maxHistory:
            return
        logInfo("[speedy] Trimming backup history (> limit).")
        working = [entry for entry in history if entry["status"] == "working"]
        others = [entry for entry in history if entry["status"] != "working"]
        if len(working) > self.maxWorking:
            for stale in working[:-self.maxWorking]:
                self.deleteState(stale["state_dir"])
            working = working[-self.maxWorking:]
        allowedOthers = max(0, self.maxHistory - len(working))
        if len(others) > allowedOthers:
            for stale in (others[:-allowedOthers] if allowedOthers else others):
                self.deleteState(stale["state_dir"])
            others = others[-allowedOthers:] if allowedOthers else []
        self.meta["history"] = sorted(working + others, key=lambda entry: entry["timestamp"])

    def deleteState(self, stateName: str) -> None:
        if self.diagMode:
            return
        folder = self.backupDir / stateName
        if folder.exists():
            try:
                shutil.rmtree(folder)
                logInfo(f"[speedy] Pruned state folder '{stateName}'.")
            except Exception:
                pass

    def markWorking(self, build: Optional[SpeedyBuild] = None) -> None:
        if not self.meta["history"]:
            return
        active = self.meta["history"][-1]
        active["status"] = "working"
        self.meta["last_working_64_dll"] = active["dll_64"]
        self.meta["failure_count"] = 0
        self.writeMeta()
        logInfo(f"[speedy] Build '{active['dll_64']}' validated WORKING; failures reset.")

    def handleFailure(self, build: Optional[SpeedyBuild] = None) -> RecoveryAction:
        if not self.meta["history"]:
            return RecoveryAction("none")
        active = self.meta["history"][-1]
        active["status"] = "faulty"
        self.meta["failure_count"] += 1
        failures = self.meta["failure_count"]
        self.writeMeta()
        logInfo(f"[speedy] Build '{active['dll_64']}' marked FAULTY ({failures}/{self.maxFailures}).")

        if failures < self.maxFailures:
            target = self.findPreviousValid()
            if target:
                logInfo(f"[speedy] Recovery: rolling back to '{target['state_dir']}'.")
                return RecoveryAction("rollback", target)
            return RecoveryAction("none")

        working = self.findLatestWorking()
        if working:
            logInfo(f"[speedy] Recovery: restoring last working state from {working['timestamp']}.")
            self.pruneAfter(working["timestamp"])
            self.meta["failure_count"] = 0
            self.writeMeta()
            return RecoveryAction("restore_working", working)

        logInfo("[speedy] Recovery: no working state; falling back to pristine.")
        self.meta["failure_count"] = 0
        self.writeMeta()
        return RecoveryAction("pristine")

    def findPreviousValid(self) -> Optional[dict]:
        for entry in reversed(self.meta["history"][:-1]):
            if entry["status"] != "faulty":
                return entry
        return None

    def findLatestWorking(self) -> Optional[dict]:
        for entry in reversed(self.meta["history"]):
            if entry["status"] == "working":
                return entry
        return None

    def pruneAfter(self, timestamp: str) -> None:
        kept = []
        for entry in self.meta["history"]:
            if entry["timestamp"] <= timestamp:
                kept.append(entry)
            else:
                self.deleteState(entry["state_dir"])
        self.meta["history"] = kept

    def applyRecovery(self, action: RecoveryAction) -> Optional[SpeedyBuild]:
        if action.kind in ("rollback", "restore_working") and action.entry:
            return self.restoreStateToSandbox(action.entry)
        if action.kind == "pristine":
            return self.restorePristineToSandbox()
        return None

    def clearSandbox(self) -> None:
        if not self.sandboxDir.exists():
            self.sandboxDir = stealth_temp.makeSessionDir(role="sandbox")
            self.sandboxMetaFile = self.sandboxDir / "stealth_meta.json"
            return
        for item in self.sandboxDir.iterdir():
            if item.name == stealth_temp.markerName:
                continue
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except Exception:
                pass

    def restoreStateToSandbox(self, entry: dict) -> Optional[SpeedyBuild]:
        stateDir = self.backupDir / entry["state_dir"]
        if not stateDir.exists():
            logInfo(f"[speedy] Restore failed: state '{entry['state_dir']}' missing.")
            return None
        self.clearSandbox()
        for item in stateDir.iterdir():
            if item.is_file():
                try:
                    shutil.copy2(item, self.sandboxDir / item.name)
                except Exception:
                    pass
        self.active64, self.active32 = entry["dll_64"], entry["dll_32"]
        self.writeSandboxMeta(entry["dll_64"], entry["dll_32"],
                              entry.get("launcher", "Speedy.exe"),
                              entry.get("bridge_64", "bridge64.exe"),
                              entry.get("bridge_32", "bridge32.exe"),
                              locateSpeedyFolder() or self.storeDir)
        logInfo(f"[speedy] Restored state '{entry['state_dir']}' to sandbox.")
        return self.findActiveBuild()

    def restorePristineToSandbox(self) -> Optional[SpeedyBuild]:
        self.clearSandbox()
        for item in self.pristineDir.iterdir():
            if item.is_file():
                try:
                    shutil.copy2(item, self.sandboxDir / item.name)
                except Exception:
                    pass
        self.active64, self.active32 = original64, original32
        self.writeSandboxMeta(original64, original32, "Speedy.exe",
                              "bridge64.exe", "bridge32.exe",
                              locateSpeedyFolder() or self.storeDir)
        logInfo("[speedy] Restored pristine (unpatched) files to sandbox.")
        return self.findActiveBuild()

    def rollbackAll(self) -> None:
        for folder, data in stealth_temp.iterOwnedFolders():
            if data.get("role") == "sandbox":
                stealth_temp.removeDir(folder)
        if self.backupDir.exists():
            for item in self.backupDir.iterdir():
                if item == self.pristineDir:
                    continue
                try:
                    item.unlink() if item.is_file() else shutil.rmtree(item)
                except Exception:
                    pass
        self.backupDir.mkdir(parents=True, exist_ok=True)
        self.pristineDir.mkdir(parents=True, exist_ok=True)
        self.meta = self.defaultMeta()
        self.writeMeta()
        self.sandboxDir = stealth_temp.makeSessionDir(role="sandbox")
        self.sandboxMetaFile = self.sandboxDir / "stealth_meta.json"
        logInfo("[speedy] ROLLBACK: cleared all sandboxes + states; pristine baseline kept.")

    def unpatch(self) -> None:
        for folder, data in stealth_temp.iterOwnedFolders():
            if data.get("role") in ("sandbox", "store"):
                stealth_temp.removeDir(folder)
        self.meta = self.defaultMeta()
        self.storeDir = stealth_temp.makeSessionDir(role="store")
        self.backupDir = self.storeDir / "backups"
        self.pristineDir = self.backupDir / "pristine"
        self.metaFile = self.backupDir / "backup_meta.json"
        self.backupDir.mkdir(parents=True, exist_ok=True)
        self.pristineDir.mkdir(parents=True, exist_ok=True)
        self.sandboxDir = stealth_temp.makeSessionDir(role="sandbox")
        self.sandboxMetaFile = self.sandboxDir / "stealth_meta.json"
        self.writeMeta()
        logInfo("[speedy] UNPATCH: temp store (incl. pristine) + sandboxes deleted; zero footprint.")

    def cleanupSession(self) -> None:
        stealth_temp.removeDir(self.sandboxDir)

    def diagnose(self, report: Callable[[str, str, str], None]) -> list[str]:
        issues: list[str] = []

        original = locateSpeedyFolder()
        if original and original.exists():
            report("speedy.locate", "PASS", str(original))
        else:
            report("speedy.locate", "FAIL", "OpenSpeedy install not found")
            issues.append("speedy install not found")

        report("speedy.store_in_temp", "PASS", f"store at {self.storeDir}")
        report("speedy.sandbox_in_temp", "PASS", f"sandbox at {self.sandboxDir}")

        try:
            probe = self.sandboxDir / "._probe.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            report("speedy.sandbox_write", "PASS", "writable")
        except Exception as error:
            report("speedy.sandbox_write", "FAIL", str(error))
            issues.append("sandbox not writable")

        if original:
            try:
                baselineReady = self.ensurePristineBaseline(original)
                report("speedy.pristine", "PASS" if baselineReady else "FAIL",
                       "baseline ready in temp" if baselineReady else "baseline incomplete")
                if not baselineReady:
                    issues.append("pristine baseline incomplete")
            except Exception as error:
                report("speedy.pristine", "FAIL", str(error))
                issues.append("pristine baseline raised")
        else:
            report("speedy.pristine", "INFO", "skipped (no install)")

        try:
            patched, changed = patchBytes(b"load speedpatch64.dll now",
                                          [(original64, "steam_api_64.dll")])
            patchWorks = changed and b"steam_api_64.dll" in patched
            report("speedy.byte_patch", "PASS" if patchWorks else "FAIL",
                   "ASCII rename works" if patchWorks else "patch produced no change")
            if not patchWorks:
                issues.append("byte patch broken")
        except Exception as error:
            report("speedy.byte_patch", "FAIL", str(error))
            issues.append("byte patch raised")

        try:
            chosen64, _chosen32 = chooseCamouflageNames({"steam_api64.dll", "unity_player.dll"})
            report("speedy.name_select", "PASS", f"chose {chosen64}")
        except Exception as error:
            report("speedy.name_select", "FAIL", str(error))
            issues.append("name select raised")

        try:
            savedMeta = copy.deepcopy(self.meta)
            self.diagMode = True
            try:
                probeEntry = {"timestamp": "9999-99-99", "state_dir": "_diag_probe_",
                              "dll_64": original64, "dll_32": original32, "status": "untested"}
                self.meta.setdefault("history", []).append(probeEntry)
                action = self.handleFailure()
                report("speedy.recovery", "PASS", f"failure path -> {action.kind}")
            finally:
                self.diagMode = False
                self.meta = savedMeta
        except Exception as error:
            report("speedy.recovery", "FAIL", str(error))
            issues.append("recovery path raised")

        return issues


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    return SpeedyDLLManager().diagnose(report)


if __name__ == "__main__":
    def printer(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    foundIssues = diagnose(printer)
    print("OK" if not foundIssues else f"ISSUES: {foundIssues}")
