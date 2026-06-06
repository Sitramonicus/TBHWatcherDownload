from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from watcher_config import logInfo


prefixPool = [
    "_MEI", "~steam", "nvstreg", "AMDCache", "msedge_cache",
    "wct", "cohtml_cache", "d3dscache", "GPUCache", "shadercache",
]

markerName = ".cacheindex"
ownerTag = "tbhw-session"

sessionDirs: set = set()
sessionIdentifier = f"{int(time.time())}_{os.getpid()}_{random.randint(1000, 9999)}"


def tempRoot() -> Path:
    return Path(tempfile.gettempdir())


def randomFolderName() -> str:
    prefix = random.choice(prefixPool)
    serial = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
    return f"{prefix}_{serial}"


def writeMarker(folder: Path, role: str) -> None:
    payload = {
        "owner": ownerTag,
        "session_id": sessionIdentifier,
        "pid": os.getpid(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "role": role,
    }
    try:
        (folder / markerName).write_text(json.dumps(payload), encoding="utf-8")
    except Exception as error:
        logInfo(f"[temp] Could not write marker in {folder}: {error}")


def readMarker(folder: Path) -> Optional[dict]:
    marker = folder / markerName
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("owner") == ownerTag:
            return data
    except Exception:
        return None
    return None


def pidAlive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


def makeSessionDir(role: str = "sandbox") -> Path:
    root = tempRoot()
    for _ in range(20):
        candidate = root / randomFolderName()
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            writeMarker(candidate, role)
            sessionDirs.add(candidate)
            logInfo(f"[temp] Created marked temp folder ({role}): '{candidate}'.")
            return candidate
    candidate = Path(tempfile.mkdtemp(prefix="_MEI_", dir=str(root)))
    writeMarker(candidate, role)
    sessionDirs.add(candidate)
    logInfo(f"[temp] Created marked temp folder via mkdtemp ({role}): '{candidate}'.")
    return candidate


def iterOwnedFolders() -> list:
    owned: list = []
    try:
        for item in tempRoot().iterdir():
            if not item.is_dir():
                continue
            data = readMarker(item)
            if data is not None:
                owned.append((item, data))
    except Exception as error:
        logInfo(f"[temp] Error scanning temp root: {error}")
    return owned


def sweepOrphans() -> int:
    removed = 0
    for folder, data in iterOwnedFolders():
        if folder in sessionDirs:
            continue
        if data.get("session_id") == sessionIdentifier:
            continue
        pid = int(data.get("pid", 0) or 0)
        if pid and pidAlive(pid):
            continue
        try:
            shutil.rmtree(folder)
            removed += 1
            logInfo(f"[temp] Swept orphaned temp folder from dead session: '{folder}'.")
        except Exception as error:
            logInfo(f"[temp] Could not remove orphan {folder}: {error}")
    if removed:
        logInfo(f"[temp] Orphan sweep removed {removed} folder(s).")
    return removed


def removeDir(folder: Path) -> bool:
    try:
        if folder.exists():
            shutil.rmtree(folder)
        sessionDirs.discard(folder)
        return True
    except Exception as error:
        logInfo(f"[temp] Could not remove {folder}: {error}")
        return False


def cleanupSession() -> int:
    removed = 0
    for folder in list(sessionDirs):
        if removeDir(folder):
            removed += 1
    if removed:
        logInfo(f"[temp] Session cleanup removed {removed} folder(s).")
    return removed


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    issues: list[str] = []
    try:
        folder = makeSessionDir(role="diag")
        marked = readMarker(folder) is not None
        report("temp.create_and_mark", "PASS" if marked else "FAIL", str(folder))
        if not marked:
            issues.append("temp marker not written")
        discoverable = any(owned == folder for owned, _ in iterOwnedFolders())
        report("temp.discoverable", "PASS" if discoverable else "FAIL",
               "marker scan found it" if discoverable else "not found by scan")
        if not discoverable:
            issues.append("owned folder not discoverable")
        removed = removeDir(folder)
        report("temp.remove", "PASS" if removed and not folder.exists() else "FAIL",
               "removed" if removed else "remove failed")
        if not removed:
            issues.append("temp remove failed")
    except Exception as error:
        report("temp.lifecycle", "FAIL", str(error))
        issues.append("temp lifecycle raised")
    return issues


if __name__ == "__main__":
    def printer(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    foundIssues = diagnose(printer)
    print("OK" if not foundIssues else f"ISSUES: {foundIssues}")
