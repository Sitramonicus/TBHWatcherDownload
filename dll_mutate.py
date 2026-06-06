from __future__ import annotations

import hashlib
import os
import random
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable

from watcher_config import logInfo

try:
    import pefile
    havePefile = True
except Exception:
    havePefile = False


overlayMagic = b"\x00\x00BUILDSTAMP\x00"


def randomOverlay() -> bytes:
    size = random.randint(192, 768)
    blob = os.urandom(size)
    return overlayMagic + struct.pack("<I", int(time.time())) + blob


def fileHash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except Exception:
        return ""
    return digest.hexdigest()


def applyOverlay(path: Path) -> bool:
    try:
        with open(path, "ab") as handle:
            handle.write(randomOverlay())
        return True
    except Exception as error:
        logInfo(f"[mutate] overlay append failed: {error}")
        return False


def applyTimestampRewrite(path: Path) -> bool:
    try:
        original = path.read_bytes()
        portableExecutable = pefile.PE(data=original, fast_load=True)
        portableExecutable.FILE_HEADER.TimeDateStamp = random.randint(0x40000000, 0x7FFFFFFF)
        rebuilt = portableExecutable.write()
        portableExecutable.close()
        if bytes(rebuilt) == original:
            return False
        path.write_bytes(bytes(rebuilt))
        return True
    except Exception as error:
        logInfo(f"[mutate] pefile timestamp pass failed ({error}); overlay still applied.")
        return False


def mutateDll(path: Path, usePefile: bool = False) -> bool:
    if not path.exists():
        logInfo(f"[mutate] cannot mutate, file missing: {path}")
        return False

    before = fileHash(path)
    overlayApplied = applyOverlay(path)

    timestampApplied = False
    if usePefile and havePefile:
        logInfo(f"[mutate] {path.name}: attempting optional pefile timestamp rewrite...")
        try:
            timestampApplied = applyTimestampRewrite(path)
        except BaseException as error:
            logInfo(f"[mutate] {path.name}: pefile pass raised hard ({error}); overlay kept.")
            timestampApplied = False

    after = fileHash(path)
    changed = (overlayApplied or timestampApplied) and before != after and after != ""
    if changed:
        method = "+".join(
            label for label, applied in (("overlay", overlayApplied), ("pefile", timestampApplied))
            if applied
        )
        logInfo(f"[mutate] {path.name}: hash changed ({before[:8]}... -> {after[:8]}...) via {method}.")
    else:
        logInfo(f"[mutate] {path.name}: hash UNCHANGED after mutation attempt.")
    return changed


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    issues: list[str] = []
    report("mutate.pefile", "INFO",
           "pefile available (opt-in; off by default)" if havePefile
           else "pefile not installed (overlay-only)")
    try:
        probe = Path(tempfile.gettempdir()) / "._tbh_mutate_probe.bin"
        probe.write_bytes(b"MZ" + os.urandom(256))
        before = fileHash(probe)
        applyOverlay(probe)
        after = fileHash(probe)
        changed = before != after
        report("mutate.hash_changes", "PASS" if changed else "FAIL",
               f"{before[:8]}... -> {after[:8]}...")
        if not changed:
            issues.append("mutation did not change hash")
        probe.unlink()
    except Exception as error:
        report("mutate.hash_changes", "FAIL", str(error))
        issues.append("mutation probe raised")
    return issues


if __name__ == "__main__":
    def printer(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    foundIssues = diagnose(printer)
    print("OK" if not foundIssues else f"ISSUES: {foundIssues}")
