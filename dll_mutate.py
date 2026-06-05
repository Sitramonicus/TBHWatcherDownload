from __future__ import annotations

import hashlib
import os
import random
import struct
import time
from pathlib import Path
from typing import Callable

from watcher_config import log

try:
    import pefile  # type: ignore
    HAVE_PEFILE = True
except Exception:
    HAVE_PEFILE = False


OVERLAY_SIGNATURE = b"\x00\x00BUILDSTAMP\x00"
MIN_OVERLAY_BYTES = 192
MAX_OVERLAY_BYTES = 768
HASH_READ_CHUNK_BYTES = 65536
EARLIEST_FAKE_TIMESTAMP = 0x40000000
LATEST_FAKE_TIMESTAMP = 0x7FFFFFFF


def _build_random_overlay() -> bytes:
    payload_size = random.randint(MIN_OVERLAY_BYTES, MAX_OVERLAY_BYTES)
    random_payload = os.urandom(payload_size)
    timestamp = struct.pack("<I", int(time.time()))
    return OVERLAY_SIGNATURE + timestamp + random_payload


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_READ_CHUNK_BYTES), b""):
                digest.update(chunk)
    except Exception:
        return ""
    return digest.hexdigest()


def _append_random_overlay(path: Path) -> bool:
    try:
        with open(path, "ab") as handle:
            handle.write(_build_random_overlay())
        return True
    except Exception as error:
        log(f"[mutate] Overlay append failed: {error}")
        return False


def _rewrite_pe_timestamp(path: Path) -> bool:
    try:
        original_bytes = path.read_bytes()
        portable_executable = pefile.PE(data=original_bytes, fast_load=True)
        portable_executable.FILE_HEADER.TimeDateStamp = random.randint(
            EARLIEST_FAKE_TIMESTAMP, LATEST_FAKE_TIMESTAMP
        )
        rewritten_bytes = portable_executable.write()
        portable_executable.close()
        if bytes(rewritten_bytes) == original_bytes:
            return False
        path.write_bytes(bytes(rewritten_bytes))
        return True
    except Exception as error:
        log(f"[mutate] pefile timestamp pass failed ({error}); overlay still applied.")
        return False


def mutate_dll(path: Path, use_pefile: bool = False) -> bool:
    if not path.exists():
        log(f"[mutate] Cannot mutate, file missing: {path}")
        return False

    hash_before = file_hash(path)

    overlay_applied = _append_random_overlay(path)

    timestamp_applied = False
    if use_pefile and HAVE_PEFILE:
        log(f"[mutate] {path.name}: attempting optional pefile timestamp rewrite...")
        try:
            timestamp_applied = _rewrite_pe_timestamp(path)
        except BaseException as error:
            log(f"[mutate] {path.name}: pefile pass raised hard ({error}); overlay kept.")
            timestamp_applied = False

    hash_after = file_hash(path)
    hash_changed = (overlay_applied or timestamp_applied) and hash_before != hash_after and hash_after != ""

    if hash_changed:
        methods_used = "+".join(
            name for name, applied in (("overlay", overlay_applied), ("pefile", timestamp_applied)) if applied
        )
        log(f"[mutate] {path.name}: hash changed "
            f"({hash_before[:8]}... -> {hash_after[:8]}...) via {methods_used}.")
    else:
        log(f"[mutate] {path.name}: hash UNCHANGED after mutation attempt.")
    return hash_changed


def diagnose(report: Callable[[str, str, str], None]) -> list[str]:
    import tempfile
    problems: list[str] = []
    report("mutate.pefile", "INFO",
           "pefile available (opt-in; off by default)" if HAVE_PEFILE
           else "pefile not installed (overlay-only)")
    try:
        probe_file = Path(tempfile.gettempdir()) / "._tbh_mutate_probe.bin"
        probe_file.write_bytes(b"MZ" + os.urandom(256))
        hash_before = file_hash(probe_file)
        _append_random_overlay(probe_file)
        hash_after = file_hash(probe_file)
        changed = hash_before != hash_after
        report("mutate.hash_changes", "PASS" if changed else "FAIL",
               f"{hash_before[:8]}... -> {hash_after[:8]}...")
        if not changed:
            problems.append("mutation did not change hash")
        probe_file.unlink()
    except Exception as error:
        report("mutate.hash_changes", "FAIL", str(error))
        problems.append("mutation probe raised")
    return problems


if __name__ == "__main__":
    def _print_row(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    found = diagnose(_print_row)
    print("OK" if not found else f"ISSUES: {found}")
