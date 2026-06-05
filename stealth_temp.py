from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from watcher_config import log

BELIEVABLE_FOLDER_PREFIXES = [
    "_MEI", "~steam", "nvstreg", "AMDCache", "msedge_cache",
    "wct", "cohtml_cache", "d3dscache", "GPUCache", "shadercache",
]
MARKER_NAME = ".cacheindex"
OWNER_TAG = "tbhw-session"
SERIAL_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
SERIAL_LENGTH = 6
UNIQUE_NAME_ATTEMPTS = 20
FALLBACK_PREFIX = "_MEI_"

_folders_created_this_session: set[Path] = set()
_session_id = f"{int(time.time())}_{os.getpid()}_{random.randint(1000, 9999)}"


def _system_temp_root() -> Path:
    return Path(tempfile.gettempdir())


def _generate_folder_name() -> str:
    prefix = random.choice(BELIEVABLE_FOLDER_PREFIXES)
    serial = "".join(random.choices(SERIAL_ALPHABET, k=SERIAL_LENGTH))
    return f"{prefix}_{serial}"


def _write_ownership_marker(folder: Path, role: str) -> None:
    marker_contents = {
        "owner": OWNER_TAG,
        "session_id": _session_id,
        "pid": os.getpid(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "role": role,
    }
    try:
        (folder / MARKER_NAME).write_text(json.dumps(marker_contents), encoding="utf-8")
    except Exception as error:
        log(f"[temp] Could not write marker in {folder}: {error}")


def _read_ownership_marker(folder: Path) -> Optional[dict]:
    marker_file = folder / MARKER_NAME
    if not marker_file.exists():
        return None
    try:
        contents = json.loads(marker_file.read_text(encoding="utf-8"))
        if isinstance(contents, dict) and contents.get("owner") == OWNER_TAG:
            return contents
    except Exception:
        return None
    return None


def _process_is_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


def make_session_dir(role: str = "sandbox") -> Path:
    temp_root = _system_temp_root()
    for _ in range(UNIQUE_NAME_ATTEMPTS):
        candidate = temp_root / _generate_folder_name()
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            _write_ownership_marker(candidate, role)
            _folders_created_this_session.add(candidate)
            log(f"[temp] Created marked temp folder ({role}): '{candidate}'.")
            return candidate
    candidate = Path(tempfile.mkdtemp(prefix=FALLBACK_PREFIX, dir=str(temp_root)))
    _write_ownership_marker(candidate, role)
    _folders_created_this_session.add(candidate)
    log(f"[temp] Created marked temp folder via mkdtemp ({role}): '{candidate}'.")
    return candidate


def iter_owned_folders() -> list[tuple[Path, dict]]:
    owned: list[tuple[Path, dict]] = []
    try:
        for item in _system_temp_root().iterdir():
            if not item.is_dir():
                continue
            marker = _read_ownership_marker(item)
            if marker is not None:
                owned.append((item, marker))
    except Exception as error:
        log(f"[temp] Error scanning temp root: {error}")
    return owned


def sweep_orphans() -> int:
    removed_count = 0
    for folder, marker in iter_owned_folders():
        if folder in _folders_created_this_session:
            continue
        if marker.get("session_id") == _session_id:
            continue
        owning_pid = int(marker.get("pid", 0) or 0)
        if owning_pid and _process_is_alive(owning_pid):
            continue
        try:
            shutil.rmtree(folder)
            removed_count += 1
            log(f"[temp] Swept orphaned temp folder from dead session: '{folder}'.")
        except Exception as error:
            log(f"[temp] Could not remove orphan {folder}: {error}")
    if removed_count:
        log(f"[temp] Orphan sweep removed {removed_count} folder(s).")
    return removed_count


def remove_dir(folder: Path) -> bool:
    try:
        if folder.exists():
            shutil.rmtree(folder)
        _folders_created_this_session.discard(folder)
        return True
    except Exception as error:
        log(f"[temp] Could not remove {folder}: {error}")
        return False


def cleanup_session() -> int:
    removed_count = 0
    for folder in list(_folders_created_this_session):
        if remove_dir(folder):
            removed_count += 1
    if removed_count:
        log(f"[temp] Session cleanup removed {removed_count} folder(s).")
    return removed_count


def session_id() -> str:
    return _session_id


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    problems: list[str] = []
    try:
        folder = make_session_dir(role="diag")
        is_marked = _read_ownership_marker(folder) is not None
        report("temp.create_and_mark", "PASS" if is_marked else "FAIL", str(folder))
        if not is_marked:
            problems.append("temp marker not written")

        discoverable = any(found == folder for found, _ in iter_owned_folders())
        report("temp.discoverable", "PASS" if discoverable else "FAIL",
               "marker scan found it" if discoverable else "not found by scan")
        if not discoverable:
            problems.append("owned folder not discoverable")

        removed = remove_dir(folder)
        report("temp.remove", "PASS" if removed and not folder.exists() else "FAIL",
               "removed" if removed else "remove failed")
        if not removed:
            problems.append("temp remove failed")
    except Exception as error:
        report("temp.lifecycle", "FAIL", str(error))
        problems.append("temp lifecycle raised")
    return problems


if __name__ == "__main__":
    def _print_row(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    found = diagnose(_print_row)
    print("OK" if not found else f"ISSUES: {found}")
