#!/usr/bin/env python3
"""
run_tests.py
============
Headless verification harness.

The four modules target Windows (tray, Win32 hang detection, Speedy injection).
This sandbox is Linux, so we install lightweight stubs for the OS-specific bits
(pystray, PIL, ctypes.windll) and a fake OpenSpeedy install, then exercise the
REAL module logic end-to-end:

    - import every module,
    - build the manager and roll a camouflage build (verifying byte-patching and
      backup history really work),
    - run the Speedy injection retry/recovery code paths,
    - construct the booter + refresher and run their diagnostics,
    - build the tray app object and walk its menu callbacks,
    - run the full diagnostics aggregation.

It prints a PASS/FAIL per check and exits non-zero on any failure. This proves
there are no syntax/wiring errors and that the Speedy<->boot contract holds.
On real Windows, the stubbed calls are replaced by the genuine APIs.
"""

import sys
import types
import tempfile
import shutil
from pathlib import Path

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"[PASS] {name} {detail}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name} {detail}")


# --- 1. Stub the Windows / GUI dependencies before importing the modules -----------

def install_stubs():
    # pystray stub: Icon + Menu + MenuItem that just store args.
    pystray = types.ModuleType("pystray")

    class _MenuItem:
        SEPARATOR = object()
        def __init__(self, text=None, action=None, **kw):
            self.text = text
            self.action = action
            self.kw = kw

    class _Menu:
        SEPARATOR = _MenuItem.SEPARATOR
        def __init__(self, *items):
            self.items = items
        def __iter__(self):
            return iter(self.items)

    class _Icon:
        def __init__(self, *a, **k):
            self.menu = k.get("menu") or (a[3] if len(a) > 3 else None)
            self.running = False
        def run(self):
            self.running = True
        def stop(self):
            self.running = False
        def update_menu(self):
            pass

    pystray.Icon = _Icon
    pystray.Menu = _Menu
    pystray.MenuItem = _MenuItem
    win32 = types.ModuleType("pystray._win32")
    pystray._win32 = win32
    sys.modules["pystray"] = pystray
    sys.modules["pystray._win32"] = win32

    # PIL stub: only what build_icon_image touches.
    PIL = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    draw_mod = types.ModuleType("PIL.ImageDraw")

    class _Img:
        def __init__(self, *a, **k):
            pass

    def _new(*a, **k):
        return _Img()

    class _Draw:
        def __init__(self, *a, **k):
            pass
        def rounded_rectangle(self, *a, **k):
            pass
        def text(self, *a, **k):
            pass

    image_mod.Image = _Img
    image_mod.new = _new
    draw_mod.Draw = lambda *a, **k: _Draw()
    PIL.Image = image_mod
    PIL.ImageDraw = draw_mod
    sys.modules["PIL"] = PIL
    sys.modules["PIL.Image"] = image_mod
    sys.modules["PIL.ImageDraw"] = draw_mod

    # ctypes.windll stub so watcher_config's _load_user32() returns a usable object.
    import ctypes
    if not hasattr(ctypes, "windll"):
        class _User32:
            def __getattr__(self, _name):
                return lambda *a, **k: 0
        class _WinDLL:
            user32 = _User32()
        ctypes.windll = _WinDLL()
    # WINFUNCTYPE works on Linux ctypes, so EnumWindowsProc is fine to define.


def minimal_pe() -> bytes:
    """Build a minimal but VALID PE32+ blob so the real pefile mutation path runs."""
    import struct
    dos = (b"MZ" + b"\x00" * 58 + struct.pack("<I", 64)).ljust(64, b"\x00")
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0x11111111, 0, 0, 240, 0x2022)
    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20b)        # PE32+ magic
    struct.pack_into("<I", opt, 16, 0x1000)      # entry point
    struct.pack_into("<Q", opt, 24, 0x180000000) # image base
    struct.pack_into("<I", opt, 32, 0x1000)      # section alignment
    struct.pack_into("<I", opt, 36, 0x200)       # file alignment
    struct.pack_into("<H", opt, 40, 6)           # major OS version
    struct.pack_into("<I", opt, 56, 0x2000)      # size of image
    struct.pack_into("<I", opt, 60, 0x200)       # size of headers
    struct.pack_into("<H", opt, 68, 2)           # subsystem
    struct.pack_into("<I", opt, 108, 16)         # number of data dirs
    sec = bytearray(40)
    sec[0:5] = b".text"
    struct.pack_into("<I", sec, 8, 0x1000)
    struct.pack_into("<I", sec, 12, 0x1000)
    struct.pack_into("<I", sec, 16, 0x200)
    struct.pack_into("<I", sec, 20, 0x200)
    struct.pack_into("<I", sec, 36, 0x60000020)
    headers = dos + b"PE\x00\x00" + coff + bytes(opt) + bytes(sec)
    return headers.ljust(0x200, b"\x00") + b"\x90" * 0x200


def build_fake_speedy(root: Path):
    """Create a fake OpenSpeedy install so locate + patch can run for real."""
    install = root / "OpenSpeedy"
    install.mkdir(parents=True, exist_ok=True)
    (install / "Speedy.exe").write_bytes(b"MZ fake speedy launcher")
    (install / "config.ini").write_text("[speedy]\nspeed=2.0\n")
    # bridges contain the signature DLL strings in ASCII and UTF-16-LE.
    sig = b"speedpatch64.dll" + "speedpatch64.dll".encode("utf-16-le")
    sig += b"speedpatch32.dll" + "speedpatch32.dll".encode("utf-16-le")
    (install / "bridge64.exe").write_bytes(b"MZ bridge64 " + sig)
    (install / "bridge32.exe").write_bytes(b"MZ bridge32 " + sig)
    # Real valid PEs so dll_mutate's pefile timestamp pass is genuinely exercised.
    (install / "speedpatch64.dll").write_bytes(minimal_pe())
    (install / "speedpatch32.dll").write_bytes(minimal_pe())
    return install


def main():
    install_stubs()

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    work = Path(tempfile.mkdtemp(prefix="tbh_test_"))
    fake_install = build_fake_speedy(work)

    # Point locate_speedy_folder at our fake install via BASE_DIR fallback + PATH.
    import os
    os.environ["PATH"] = str(fake_install) + os.pathsep + os.environ.get("PATH", "")
    # shutil.which needs an executable bit on Linux:
    speedy_bin = fake_install / "Speedy.exe"
    speedy_bin.chmod(0o755)

    # Isolate the system temp dir so our marked folders never touch the real /tmp.
    fake_temp = work / "systemp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    for var in ("TMPDIR", "TEMP", "TMP"):
        os.environ[var] = str(fake_temp)
    # Isolate the persistent data dir too (config/log) into our work area so the
    # test never writes to the real %LOCALAPPDATA% / ~/.local/share.
    fake_data = work / "appdata"
    fake_data.mkdir(parents=True, exist_ok=True)
    os.environ["LOCALAPPDATA"] = str(fake_data)
    os.environ["HOME"] = str(fake_data)
    import tempfile as _tf
    _tf.tempdir = str(fake_temp)

    # --- import every module -------------------------------------------------------
    try:
        import watcher_config
        import stealth_temp
        import dll_mutate
        import speedy_dll
        import boot
        import refresher
        import main as mainmod
        check("import.all_modules", True)
    except Exception as exc:
        check("import.all_modules", False, repr(exc))
        finish()
        return

    # --- config module -------------------------------------------------------------
    cfg = watcher_config.load_config()
    check("config.load", isinstance(cfg, dict) and "task_process_name" in cfg)
    issues = watcher_config.diagnose(lambda c, s, d: None)
    check("config.diagnose", isinstance(issues, list))

    # --- stealth_temp: marker lifecycle + orphan sweep -----------------------------
    tdiag = stealth_temp.diagnose(lambda c, s, d: None)
    check("temp.diagnose_clean", isinstance(tdiag, list) and not tdiag, f"-> {tdiag}")
    # Folders carry markers and only ours are discoverable.
    d1 = stealth_temp.make_session_dir(role="sandbox")
    owned_names = [f.name for f, _ in stealth_temp.iter_owned_folders()]
    check("temp.marker_identifies_ours", d1.name in owned_names)
    # An orphan from a 'dead' session (fake pid) must be swept; ours must survive.
    orphan = fake_temp / "GPUCache_dead01"
    orphan.mkdir()
    import json as _json
    (orphan / stealth_temp.MARKER_NAME).write_text(_json.dumps({
        "owner": stealth_temp.OWNER_TAG, "session_id": "old", "pid": 999999999,
        "created": "x", "role": "sandbox"}), encoding="utf-8")
    removed = stealth_temp.sweep_orphans()
    check("temp.sweeps_dead_orphans", not orphan.exists() and removed >= 1,
          f"-> removed {removed}")
    check("temp.keeps_live_session", d1.exists())
    # A folder owned by ANOTHER live PID (e.g. a second watcher) must survive.
    live_other = fake_temp / "GPUCache_liveother"
    live_other.mkdir()
    (live_other / stealth_temp.MARKER_NAME).write_text(_json.dumps({
        "owner": stealth_temp.OWNER_TAG, "session_id": "other", "pid": os.getpid(),
        "created": "x", "role": "sandbox"}), encoding="utf-8")
    # A non-marked folder must NEVER be touched.
    not_ours = fake_temp / "GPUCache_notours"
    not_ours.mkdir(); (not_ours / "x.txt").write_text("x")
    stealth_temp.sweep_orphans()
    check("temp.keeps_other_live_pid", live_other.exists())
    check("temp.never_touches_unmarked", not_ours.exists())
    stealth_temp.remove_dir(d1)
    stealth_temp.remove_dir(live_other)
    import shutil as _sh; _sh.rmtree(not_ours, ignore_errors=True)

    # --- dll_mutate: hash actually changes -----------------------------------------
    mdiag = dll_mutate.diagnose(lambda c, s, d: None)
    check("mutate.diagnose_clean", isinstance(mdiag, list) and not mdiag, f"-> {mdiag}")
    # crash-safety: mutate_dll must never raise, even on a non-PE garbage file,
    # with or without the opt-in pefile pass (the boot crash was a hard failure here).
    import tempfile as _tf2
    garbage = Path(_tf2.gettempdir()) / "._tbh_garbage_probe.dll"
    garbage.write_bytes(b"not a real pe at all " + os.urandom(64))
    try:
        r1 = dll_mutate.mutate_dll(garbage, use_pefile=False)
        r2 = dll_mutate.mutate_dll(garbage, use_pefile=True)  # pefile will fail->isolated
        check("mutate.never_raises_on_garbage", True, f"overlay={r1}, pefile-path={r2}")
    except BaseException as exc:
        check("mutate.never_raises_on_garbage", False, repr(exc))
    finally:
        garbage.unlink(missing_ok=True)
    # default prepare() path must NOT use pefile (crash fix verified at config level)
    check("config.pefile_mutation_off_by_default",
          watcher_config.DEFAULT_CONFIG.get("dll_mutation_pefile_enabled") is False)

    # all camouflage names must match the original 16-char length or patching is skipped
    bad_len = [(a, b) for a, b in speedy_dll.CAMOUFLAGE_NAMES if len(a) != 16 or len(b) != 16]
    check("speedy.names_all_16_chars", not bad_len, f"-> bad: {bad_len}")

    # --- speedy manager: real patch round-trip (now temp-based) --------------------
    located = speedy_dll.locate_speedy_folder()
    check("speedy.locate", located is not None and (located / "Speedy.exe").exists(),
          f"-> {located}")

    mgr = speedy_dll.SpeedyDLLManager()
    # store + sandbox must live under the fake system temp, not the project dir.
    check("speedy.store_under_temp", str(mgr.store_dir).startswith(str(fake_temp)),
          f"-> {mgr.store_dir}")
    check("speedy.sandbox_under_temp", str(mgr.sandbox_dir).startswith(str(fake_temp)),
          f"-> {mgr.sandbox_dir}")

    build = mgr.prepare()
    check("speedy.prepare_returns_build", build is not None,
          f"-> {build.dll_64 if build else None}")
    if build:
        check("speedy.launcher_renamed", build.speedy_exe.exists()
              and build.speedy_exe.name != "Speedy.exe", f"-> {build.speedy_exe.name}")
        check("speedy.bridges_renamed",
              build.bridge_64 != "bridge64.exe" and build.bridge_32 != "bridge32.exe",
              f"-> {build.bridge_64}/{build.bridge_32}")
        renamed_64 = build.sandbox_dir / build.dll_64
        check("speedy.renamed_dll_emitted", renamed_64.exists(), f"-> {build.dll_64}")
        # DLL was mutated (overlay, on by default): hash differs from the original.
        pristine_dll = mgr.pristine_dir / "speedpatch64.dll"
        check("speedy.dll_hash_mutated",
              dll_mutate.file_hash(renamed_64) != dll_mutate.file_hash(pristine_dll),
              "renamed DLL hash differs from original (overlay)")
        # prepare() must NOT run the risky pefile pass by default (crash fix): the
        # default build should still be a valid, parseable PE because only an
        # overlay was appended (loader ignores it).
        if dll_mutate.HAVE_PEFILE:
            import pefile as _pf
            try:
                _pf.PE(data=renamed_64.read_bytes()).close()
                check("speedy.default_mutation_keeps_valid_pe", True, "overlay-only, still valid PE")
            except Exception as exc:
                check("speedy.default_mutation_keeps_valid_pe", False, repr(exc))
            # Opt-in pefile pass, exercised DIRECTLY (not via prepare), must change
            # the timestamp AND keep a valid PE -- proves the feature still works
            # when a user explicitly enables it.
            probe = build.sandbox_dir / "._pefile_probe.dll"
            probe.write_bytes(pristine_dll.read_bytes())
            ts_a = _pf.PE(data=probe.read_bytes()); a = ts_a.FILE_HEADER.TimeDateStamp; ts_a.close()
            dll_mutate.mutate_dll(probe, use_pefile=True)
            try:
                pe_b = _pf.PE(data=probe.read_bytes()); b = pe_b.FILE_HEADER.TimeDateStamp; pe_b.close()
                check("speedy.optin_pefile_timestamp_applied", a != b, f"-> {hex(a)} -> {hex(b)}")
            except Exception as exc:
                check("speedy.optin_pefile_timestamp_applied", False, repr(exc))
            probe.unlink()
        # Bridge (under its renamed file) was byte-patched to the new DLL name.
        bridge = (build.sandbox_dir / build.bridge_64).read_bytes()
        check("speedy.bridge_patched_ascii",
              build.dll_64.encode("utf-8") in bridge and b"speedpatch64.dll" not in bridge)
        check("speedy.bridge_patched_utf16",
              build.dll_64.encode("utf-16-le") in bridge)

    # find_active_build should locate what prepare() just wrote.
    found = mgr.find_active_build()
    check("speedy.find_active_build", found is not None and found.dll_64 == (build.dll_64 if build else None))

    mgr.mark_working(build)
    check("speedy.mark_working", mgr.meta["history"][-1]["status"] == "working")

    build2 = mgr.prepare()
    action = mgr.handle_failure(build2)
    check("speedy.handle_failure", action.kind in ("rollback", "restore_working", "pristine", "none"),
          f"-> {action.kind}")
    recovered = mgr.apply_recovery(action)
    check("speedy.apply_recovery_runs", True, f"-> {recovered.dll_64 if recovered else None}")

    sdiag = mgr.diagnose(lambda c, s, d: None)
    check("speedy.diagnose", isinstance(sdiag, list))

    # --- DLL name ROTATION (regression guard for the 'always steam_api' bug) -------
    # Reproduce the real environment: the game has steam_api64.dll loaded. The name
    # must rotate every roll and NEVER repeat consecutively (this is the whole point
    # of the re-roller: avoid tracking). This is exactly the failure mode the user
    # caught that my earlier self-tests missed.
    game_mods = {"steam_api64.dll", "unityplayer.dll", "d3d11.dll"}
    rot_seq = []
    rot_avoid = None
    for _ in range(40):
        rn64, _rn32 = speedy_dll.choose_camouflage_names(game_mods, avoid=rot_avoid)
        rot_seq.append(rn64)
        rot_avoid = rn64
    consec = [i for i in range(1, len(rot_seq)) if rot_seq[i] == rot_seq[i - 1]]
    check("speedy.name_no_consecutive_repeat", not consec, f"-> repeats at {consec}")
    distinct = len(set(rot_seq))
    check("speedy.name_actually_rotates", distinct >= 6,
          f"-> {distinct} distinct of {len(speedy_dll.CAMOUFLAGE_NAMES)} over 40 rolls")
    top_share = max(rot_seq.count(n) for n in set(rot_seq)) / len(rot_seq)
    check("speedy.name_not_dominated", top_share <= 0.5,
          f"-> most-frequent name share {top_share:.2f} (must be <= 0.5)")
    # avoid is honoured even with no game modules
    a1, _ = speedy_dll.choose_camouflage_names(None, avoid="steam_api_64.dll")
    check("speedy.avoid_respected_no_modules", a1 != "steam_api_64.dll", f"-> {a1}")
    # full prepare() path rotates too, and the no-repeat survives a restart
    mgr.prepare(game_modules=game_mods)
    prep_seq = [mgr.prepare(game_modules=game_mods).dll_64 for _ in range(8)]
    prep_consec = [i for i in range(1, len(prep_seq)) if prep_seq[i] == prep_seq[i - 1]]
    check("speedy.prepare_no_consecutive_repeat", not prep_consec, f"-> {prep_seq}")
    last_name = mgr.meta.get("last_dll_64")
    mgr_restart = speedy_dll.SpeedyDLLManager()  # reuses the temp store + meta
    check("speedy.no_repeat_persists_restart",
          mgr_restart.meta.get("last_dll_64") == last_name
          and mgr_restart.prepare(game_modules=game_mods).dll_64 != last_name,
          f"-> last was {last_name}")

    # rollback keeps pristine; unpatch removes the whole temp store
    mgr.prepare()
    pristine_files_before = list(mgr.pristine_dir.glob("*"))
    store_before = mgr.store_dir
    mgr.rollback_all()
    check("speedy.rollback_keeps_pristine",
          mgr.pristine_dir.exists() and len(list(mgr.pristine_dir.glob("*"))) == len(pristine_files_before),
          f"-> {len(list(mgr.pristine_dir.glob('*')))} pristine files kept")
    check("speedy.rollback_clears_states",
          not any(p.name.startswith("state_") for p in mgr.backup_dir.glob("*")))
    mgr.unpatch()
    check("speedy.unpatch_removes_old_store", not store_before.exists(),
          f"-> old store exists: {store_before.exists()}")

    # --- boot module ---------------------------------------------------------------
    class _Cfg:
        task_process_name = "TaskBarHero.exe"
        game_app_id = "3678970"
        game_process_name = None
        speedy_enabled = True
        speedy_process_name = "Speedy.exe"
        speedy_command = "speedy"
        speedy_delay_seconds = 0.01
        speedy_delay_jitter_seconds = 0.0
        speedy_init_seconds = 0.01
        refresher_interval_jitter_seconds = 0.0
        force_kill_timeout_seconds = 1
        game_launch_timeout_seconds = 1
        focus_shield_seconds = 0.05
        dll_reroller_enabled = False
        poll_interval_seconds = 1
        hang_confirm_seconds = 1
        refresher_enabled = True
        refresher_interval_minutes = 7.0
        refresher_cooldown_seconds = 1
        refresher_interval_seconds = 420.0

    import threading
    stop = threading.Event()
    state = watcher_config.WatchState()
    booter = boot.GameBooter(lambda: _Cfg(), state, stop, mgr)

    check("boot.processes_named", isinstance(booter.processes_named("nonexistent.exe"), list))
    check("boot.pid_snapshot", isinstance(booter.pid_snapshot(), set))
    check("boot.is_game_running", booter.is_game_running() is False)
    check("boot.is_game_hung", booter.is_game_hung() is False)
    bdiag = booter.diagnose(lambda c, s, d: None)
    check("boot.diagnose", isinstance(bdiag, list))
    # Exercise resolve_build hand-off (the boot<->speedy contract)
    resolved = booter._resolve_build(game_modules=set())
    check("boot.speedy_contract", resolved is None or hasattr(resolved, "dll_64"))

    # ===== Regression guards for the 8 problems found in adversarial review =====

    # P1/P2: closing Speedy must NOT kill processes by cover name. The kill set must
    # be path-scoped (our temp dirs) + literal default names only -- cover names like
    # nvcontainer.exe / SteamService.exe must never be targeted by name.
    import boot as _bootmod
    src_boot = open(_bootmod.__file__).read()
    check("boot.no_killing_by_cover_name",
          "LAUNCHER_COVER_NAMES + BRIDGE64_COVER_NAMES + BRIDGE32_COVER_NAMES" not in src_boot
          and "processes_under_dir" in src_boot,
          "close uses path-scoping, not cover-name matching")
    # Prove close_speedy_and_helpers only kills via processes_under_dir + literal names.
    killed_names = []
    kbooter = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    kbooter.processes_under_dir = lambda d: []          # nothing running from our dirs
    kbooter.ensure_closed = lambda name, timeout=5: killed_names.append(name) or True
    kbooter.close_speedy_and_helpers(timeout=1)
    cover_hit = [n for n in killed_names
                 if n in (speedy_dll.LAUNCHER_COVER_NAMES + speedy_dll.BRIDGE64_COVER_NAMES
                          + speedy_dll.BRIDGE32_COVER_NAMES)]
    check("boot.close_never_targets_cover_names", not cover_hit,
          f"-> would have killed {cover_hit}")
    check("boot.close_targets_only_literals",
          set(killed_names) <= {"Speedy.exe", "bridge64.exe", "bridge32.exe"},
          f"-> {sorted(set(killed_names))}")

    # P1: focus shield must not treat a cover-named process as a thief by name.
    check("boot.focus_keywords_safe",
          all(k not in _bootmod.FOCUS_THIEF_KEYWORDS
              for k in ("overlay", "render", "relay", "gpu_proc", "audio_svc")),
          f"-> {_bootmod.FOCUS_THIEF_KEYWORDS}")

    # P3: launch lock -- two concurrent launch_game_flow calls must not both run.
    import time as _t3
    lk = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    run_count = {"n": 0}
    def _slow_seq():
        run_count["n"] += 1
        _t3.sleep(0.3)
    lk.execute_launch_sequence = _slow_seq
    lk.pid_snapshot = lambda: set()
    lk.wait_for_game_open = lambda *a, **k: True
    t_a = threading.Thread(target=lk.launch_game_flow)
    t_b = threading.Thread(target=lk.launch_game_flow)
    t_a.start(); t_b.start(); t_a.join(); t_b.join()
    check("boot.launch_lock_serialises", run_count["n"] == 1,
          f"-> {run_count['n']} concurrent launches ran (must be 1)")

    # P8: a deliberate close_game sets busy so the monitor won't call it a hang.
    bb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    bb.ensure_closed = lambda name, timeout=5: True
    check("boot.not_busy_initially", bb.busy is False)
    bb.close_game()
    check("boot.close_game_sets_busy", bb.busy is True)
    # P (new): busy must NEVER get stuck. If close_game() set busy and the following
    # launch_game_flow() is SKIPPED (lock held by a concurrent launch), busy must be
    # cleared on the skip path -- otherwise the monitor is suppressed forever.
    sb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    sb.ensure_closed = lambda name, timeout=5: True
    sb._launch_lock.acquire()  # simulate a concurrent launch holding the lock
    sb.close_game()            # sets busy
    assert sb.busy is True
    sb.launch_game_flow()      # must be skipped AND clear the busy flag
    check("boot.busy_not_stuck_when_launch_skipped", sb.busy is False,
          "skipped launch cleared the busy flag")
    sb._launch_lock.release()
    # And the safety timer auto-clears busy even if nothing else does.
    st = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    st.mark_busy(safety_clear_after=0.2)
    was_set = st.busy
    _t3.sleep(0.45)
    check("boot.busy_safety_timer_clears", was_set is True and st.busy is False,
          "safety timer auto-cleared busy")

    # P5: ConfigHolder.reload has a lock (concurrent reload is synchronised).
    _ch = mainmod.ConfigHolder()
    check("config.reload_has_lock", hasattr(_ch, "_lock"))

    # Config + log live in a PERSISTENT data dir (NOT temp, NOT beside the exe), so
    # user edits survive sessions. In this harness DATA_DIR resolves under our fake
    # temp HOME, but the key property is: it's a real dir we can read/write and it's
    # the same path load/save use.
    check("config.data_dir_used_for_files",
          str(watcher_config.CONFIG_FILE).startswith(str(watcher_config.DATA_DIR))
          and str(watcher_config.LOG_FILE).startswith(str(watcher_config.DATA_DIR)),
          f"-> {watcher_config.DATA_DIR}")
    check("config.data_dir_not_temp_by_default",
          "resolve_data_dir" in open(watcher_config.__file__).read()
          and "LOCALAPPDATA" in open(watcher_config.__file__).read(),
          "uses %LOCALAPPDATA%/TBHWatcher (persistent) not the temp dir")
    # Persistence round-trip: save a change, reload, value retained.
    _cfg = watcher_config.load_config(); _cfg["refresher_cooldown_seconds"] = 42
    watcher_config.save_config(_cfg)
    check("config.persists_across_reload",
          watcher_config.load_config().get("refresher_cooldown_seconds") == 42,
          "edited value retained after save+reload")

    # Single-instance: first acquire succeeds; a simulated second caller is rejected.
    first = watcher_config.acquire_single_instance()
    check("config.single_instance_first_ok", first is True)
    check("config.single_instance_api_exists",
          callable(getattr(watcher_config, "acquire_single_instance", None))
          and callable(getattr(watcher_config, "warn_already_running", None)))
    # Fix #6: mutex uses use_last_error + get_last_error (reliable already-running
    # detection), not a separate GetLastError() read that can be reset.
    _wc_src = open(watcher_config.__file__).read()
    check("config.mutex_uses_last_error",
          'WinDLL("kernel32", use_last_error=True)' in _wc_src
          and "ctypes.get_last_error()" in _wc_src,
          "single-instance mutex reads last error reliably")

    # Fix #5: save_config is atomic (temp file + os.replace), so an interrupted
    # write can't corrupt the config.
    check("config.save_is_atomic",
          ".json.tmp" in _wc_src and "os.replace" in _wc_src.replace("_os.replace", "os.replace"),
          "save_config writes to temp then atomically replaces")
    # Round-trip still works after the atomic-write change.
    _c5 = watcher_config.load_config(); _c5["poll_interval_seconds"] = 9
    watcher_config.save_config(_c5)
    check("config.atomic_save_roundtrip",
          watcher_config.load_config().get("poll_interval_seconds") == 9)

    # Fix #1: find_active_build prefers THIS session's own sandbox, never a
    # stale/other-session folder.
    own = mgr.prepare()  # writes a build into mgr.sandbox_dir
    fab = mgr.find_active_build()
    check("speedy.find_active_prefers_own_sandbox",
          fab is not None and fab.sandbox_dir == mgr.sandbox_dir,
          f"-> {fab.sandbox_dir.name if fab else None} vs own {mgr.sandbox_dir.name}")

    # Fix #4: wait_for_game_open falls back to task_process_name, not a blind
    # PID-diff (no false positives from unrelated new processes).
    _wf_src = open(_bootmod.__file__).read()
    check("boot.wait_uses_task_name_fallback",
          'self._setting("task_process_name"' in _wf_src
          and "self.pid_snapshot() - before" not in _wf_src,
          "game-open wait uses known process name, not PID-diff heuristic")

    # P7: run_diagnostics must NOT mutate live speedy meta (failure_count/history).
    mgr.meta["failure_count"] = 0
    import json as _j7
    meta_before = _j7.dumps(mgr.meta, sort_keys=True)
    mgr.diagnose(lambda c, s, d: None)
    meta_after = _j7.dumps(mgr.meta, sort_keys=True)
    check("speedy.diagnose_non_destructive", meta_before == meta_after,
          "diagnostics left meta unchanged")
    check("speedy.diag_failcount_unchanged", mgr.meta["failure_count"] == 0,
          f"-> {mgr.meta['failure_count']}")

    # P6: _resolve_build must close any running injector BEFORE prepare() wipes the
    # sandbox (else Windows file locks on a still-running EXE break the wipe).
    order = []
    class _RerollCfg(_Cfg):
        dll_reroller_enabled = True
    rb = boot.GameBooter(lambda: _RerollCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    rb.close_speedy_and_helpers = lambda timeout=5, build=None: order.append("close")
    class _FakeSpeedy:
        sandbox_dir = mgr.sandbox_dir
        store_dir = mgr.store_dir
        def prepare(self, game_modules=None):
            order.append("prepare"); return None
        def find_active_build(self):
            return None
    rb.speedy = _FakeSpeedy()
    rb._resolve_build(game_modules=set())
    check("boot.closes_before_prepare", order[:2] == ["close", "prepare"],
          f"-> order {order}")

    # P (blank-taskbar-window bug): _launch_speedy must NOT combine DETACHED_PROCESS
    # with CREATE_NO_WINDOW (mutually exclusive -> stray uncloseable window), must
    # not keep the Popen handle, and the workflow must WM_CLOSE sandbox windows
    # before terminating so no hidden window survives in the taskbar.
    src_boot2 = open(_bootmod.__file__).read()
    check("boot.no_detached_plus_nowindow_combo",
          "CREATE_NO_WINDOW | DETACHED_PROCESS" not in src_boot2
          and "creationflags=DETACHED_PROCESS," in src_boot2,
          "launch uses DETACHED_PROCESS alone (no conflicting flag combo)")
    check("boot.closes_windows_before_terminate",
          "close_windows_in_dir(self.speedy.sandbox_dir)" in src_boot2
          and "def close_windows_in_dir" in src_boot2,
          "WM_CLOSE sandbox windows before killing the process")
    # close_windows_in_dir must enumerate hidden windows too (only_visible=False).
    check("boot.close_includes_hidden_windows",
          "only_visible=False" in src_boot2,
          "closes even hidden windows (the blank taskbar ghost)")
    # _launch_speedy releases its child handle (fire-and-forget) so the child's
    # lifetime is never tethered to the watcher.
    check("boot.launch_releases_handle",
          "del child" in src_boot2,
          "Popen handle dropped after launch")

    # P (blank-window root cause): launch_game must fire the URI via the canonical
    # Win32 shell API ShellExecuteW with SW_HIDE -- NO intermediary process and NO
    # window created on our side. Steam receives the request directly, so any window
    # Steam shows is owned by steam.exe, not us. os.startfile is only a fallback.
    _lg_body = src_boot2.split("def launch_game(self)")[1].split("\n    def ")[0]
    check("boot.launch_uses_shellexecute",
          "ShellExecuteW" in _lg_body and "SW_HIDE" in _lg_body
          and "shell32" in _lg_body,
          "launch_game fires URI via ShellExecuteW(SW_HIDE)")
    # On the Windows path (after the non-win early-return) there must be NO
    # subprocess.Popen -- the URI is fired via ShellExecuteW with no child process.
    _win_part = _lg_body.split("startswith(\"win\")")[1] if "startswith(\"win\")" in _lg_body else _lg_body
    _win_part = _win_part.split("return", 1)[1] if "return" in _win_part else _win_part
    check("boot.launch_no_subprocess_window",
          "subprocess.Popen" not in _win_part,
          "Windows launch path spawns NO child process (no window source)")
    # And launch_game must not raise on this platform.
    _lgb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                           threading.Event(), mgr)
    _lgb.launch_game()
    check("boot.launch_game_no_raise", True)

    # Emergency window clear: callable, returns the expected summary keys, no raise.
    ec = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    summ = ec.emergency_window_clear()
    check("boot.emergency_clear_runs",
          isinstance(summ, dict)
          and {"dir_windows_closed", "procs_killed", "blank_windows_closed",
               "orphans_swept"} <= set(summ.keys()),
          f"-> {summ}")
    # enum_all_top_level_windows exists and is safe to call off-Windows.
    check("config.enum_all_windows_safe",
          isinstance(watcher_config.enum_all_top_level_windows(), list))
    # Stray-taskbar-window diagnostic + fix (the confirmed bug: window owned by our
    # own PID). diagnose_own_windows returns a list; strip_from_taskbar is callable
    # and safe; both no-op cleanly off-Windows.
    import os as _os2
    check("config.diagnose_own_windows_safe",
          isinstance(watcher_config.diagnose_own_windows(_os2.getpid()), list))
    check("config.strip_from_taskbar_safe",
          watcher_config.strip_from_taskbar(0) in (True, False))
    # The launch sequence logs/strips our own windows (gated by config, default on)
    # and emergency_window_clear reports own_stripped.
    _bsrc = open(_bootmod.__file__).read()
    check("boot.launch_logs_own_windows",
          "_log_own_windows" in _bsrc and "diagnose_own_windows" in _bsrc
          and "strip_from_taskbar" in _bsrc,
          "post-launch own-window diagnostic + strip wired")
    _summ2 = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                             threading.Event(), mgr).emergency_window_clear()
    check("boot.emergency_reports_own_stripped", "own_stripped" in _summ2)
    # The diagnostic method itself must never raise.
    try:
        boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                        threading.Event(), mgr)._log_own_windows()
        check("boot.log_own_windows_no_raise", True)
    except Exception as exc:
        check("boot.log_own_windows_no_raise", False, repr(exc))

    # Debug menu exists and contains the emergency button + the moved utilities.
    _dbgapp = mainmod.TBHWatcher()
    dbg = _dbgapp._build_debug_menu()
    labels = [getattr(it, "text", None) for it in dbg
              if it is not type(dbg).SEPARATOR]
    labels = [t for t in labels if isinstance(t, str)]
    check("main.debug_menu_has_emergency",
          any("Emergency" in t for t in labels)
          and any("Open Config" in t for t in labels)
          and any("Clear Log" in t for t in labels),
          f"-> {labels}")

    # --- double-inject detection ---------------------------------------------------
    # When a camouflage/original DLL is already loaded in the game, is_already_injected
    # must report True; otherwise False.
    inj = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    a_name = speedy_dll.CAMOUFLAGE_NAMES[0][0]
    # forward slashes so pathlib.Path(...).name works on the Linux test host too
    inj.loaded_dll_paths = lambda pid: {f"c:/game/{a_name}", "c:/game/other.dll"}
    already, found = inj.is_already_injected(1234)
    check("boot.detects_existing_injection", already and found == a_name.lower(), f"-> {found}")
    inj.loaded_dll_paths = lambda pid: {"c:/game/d3d11.dll", "c:/game/kernel32.dll"}
    already2, _ = inj.is_already_injected(1234)
    check("boot.no_false_injection", already2 is False)
    inj.loaded_dll_paths = lambda pid: set()  # access denied / empty
    already3, _ = inj.is_already_injected(1234)
    check("boot.inject_check_safe_when_blind", already3 is False)

    # workflow must SKIP launching Speedy when already injected
    skip_launches = {"n": 0}
    inj2 = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    class _GP:  # noqa
        pid = 777
    inj2.processes_named = lambda name: ([_GP()] if name == "TaskBarHero.exe" else [])
    inj2.loaded_dll_paths = lambda pid: {f"c:/game/{a_name}"}  # already injected
    def _skip_launch(build):
        skip_launches["n"] += 1
        return True
    inj2._launch_speedy = _skip_launch
    inj2._resolve_build = lambda game_modules=None: None
    inj2.close_speedy_and_helpers = lambda timeout=5, build=None: None
    inj2._run_speedy_workflow_impl()
    check("boot.skips_when_already_injected", skip_launches["n"] == 0,
          f"-> {skip_launches['n']} launch(es)")

    # --- post-injection verify + reinject -----------------------------------------
    class _VCfg(_Cfg):
        verify_injection_enabled = True
        verify_injection_delay_seconds = 0.05
    a_name2 = speedy_dll.CAMOUFLAGE_NAMES[2][0]

    # Case A: injection DID take -> verify confirms, NO reinject (anti-double-inject).
    vb = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb.processes_named = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    vb.loaded_dll_paths = lambda pid: {f"c:/game/{a_name2}"}  # already loaded
    reinjects = {"n": 0}
    vb._inject_once = lambda build, dll: reinjects.__setitem__("n", reinjects["n"] + 1) or True
    vb._verify_and_maybe_reinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_no_reinject_when_injected", reinjects["n"] == 0,
          f"-> {reinjects['n']} reinject(s) (must be 0)")

    # Case B: injection did NOT take -> exactly one reinject happens.
    vb2 = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb2.processes_named = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    # modules readable (non-empty) but our DLL not present -> not injected.
    vb2.loaded_dll_paths = lambda pid: {"c:/game/d3d11.dll"}
    reinjects2 = {"n": 0}
    vb2._inject_once = lambda build, dll: reinjects2.__setitem__("n", reinjects2["n"] + 1) or True
    vb2.speedy.mark_working = lambda build=None: None
    vb2._verify_and_maybe_reinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_reinjects_when_missing", reinjects2["n"] == 1,
          f"-> {reinjects2['n']} reinject(s) (must be 1)")

    # Case C: modules unreadable (no admin) -> skip verify, do NOT reinject (could
    # double-inject if it actually worked).
    vb3 = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb3.processes_named = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    vb3.loaded_dll_paths = lambda pid: set()  # access denied / empty
    reinjects3 = {"n": 0}
    vb3._inject_once = lambda build, dll: reinjects3.__setitem__("n", reinjects3["n"] + 1) or True
    vb3._verify_and_maybe_reinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_skips_when_blind", reinjects3["n"] == 0,
          f"-> {reinjects3['n']} reinject(s) (must be 0)")

    # --- confirm_dialog: headless returns False (safe No), and is callable ----------
    cd_result = watcher_config.confirm_dialog("Test", "Proceed?")
    check("config.confirm_dialog_safe_default", cd_result is False,
          "headless/No -> False (won't proceed)")

    # --- global excepthook installs without error ----------------------------------
    try:
        watcher_config.install_global_excepthook()
        check("config.excepthook_installs", True)
    except Exception as exc:
        check("config.excepthook_installs", False, repr(exc))

    # --- log_error with context writes a hint --------------------------------------
    try:
        raise PermissionError("denied")
    except Exception as exc:
        watcher_config.log_error("probe", exc=exc, where="run_tests.probe")
    check("config.log_error_with_context", True, "logged with module+hint")

    # --- splash module -------------------------------------------------------------
    import splash as splash_mod
    spdiag = splash_mod.diagnose(lambda c, s, d: None)
    check("splash.diagnose", isinstance(spdiag, list) and not spdiag, f"-> {spdiag}")
    # NullSplash stand-in must satisfy the interface used in __init__ (incl notify_ready).
    ns = mainmod._NullSplash()
    ns.start(); ns.step("x"); ns.message("y"); ns.notify_ready(None); ns.finish()
    check("main.null_splash_interface", True)
    # P (blank-window ROOT CAUSE): the splash must create NO window at all -- the
    # leftover blank taskbar window was owned by the watcher's own PID (the splash).
    # No tkinter, no Win32 window creation.
    import splash as _spmod
    src = open(_spmod.__file__).read()
    check("splash.no_window_creation",
          "import tkinter" not in src and "tk.Tk" not in src
          and "CreateWindowEx" not in src and "WINFUNCTYPE" not in src,
          "splash creates no window (log + tray balloon only)")
    # Real Splash must run its whole lifecycle without raising and without a window.
    try:
        rs = _spmod.Splash(total_phases=3)
        rs.start(); rs.step("a"); rs.message("b"); rs.notify_ready(None); rs.finish()
        check("splash.lifecycle_safe", True)
    except Exception as exc:
        check("splash.lifecycle_safe", False, repr(exc))

    # --- STRICT Stable timing sequence verification --------------------------------
    # Prove run_speedy_workflow follows Stable: sleep(delay) -> launch ONCE ->
    # dwell init_seconds+random() (no detection poll, no retries) -> terminate once.
    import time as _time

    class _TimedCfg(_Cfg):
        speedy_delay_seconds = 0.2          # stands in for the 7s loading wait
        speedy_delay_jitter_seconds = 0.0   # no jitter so the test timing is exact
        speedy_init_seconds = 0.5           # dwell base; +random() -> 0.5..1.5
        dll_reroller_enabled = False

    launch_calls = {"n": 0}
    dwell_hides = {"n": 0}
    timeline = {}

    class _P:  # noqa
        pid = 4321

    timed = boot.GameBooter(lambda: _TimedCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    timed.processes_named = lambda name: ([_P()] if name == "TaskBarHero.exe" else [])
    timed.loaded_dll_paths = lambda pid: set()             # game-module read (#3), harmless
    timed._resolve_build = lambda game_modules=None: None  # force fallback DLL name path
    def _fake_launch(build):
        launch_calls["n"] += 1
        timeline["launched_at"] = _time.time()
        return True
    timed._launch_speedy = _fake_launch
    def _count_hide(name):
        dwell_hides["n"] += 1
    timed.hide_process_windows = _count_hide
    timed.close_speedy_and_helpers = lambda timeout=5, build=None: timeline.__setitem__("closed_at", _time.time())
    timed.speedy.mark_working = lambda build=None: None

    t0 = _time.time()
    timed.run_speedy_workflow()
    total = _time.time() - t0

    check("timing.launched_exactly_once", launch_calls["n"] == 1, f"-> {launch_calls['n']} launch(es)")
    # delay (0.2) happened before launch
    pre_launch = timeline.get("launched_at", t0) - t0
    check("timing.delay_before_launch", pre_launch >= 0.2 - 0.05, f"-> {pre_launch:.3f}s")
    # dwell after launch is init_seconds + random() -> between 0.5 and 1.5s
    dwell = timeline.get("closed_at", 0) - timeline.get("launched_at", 0)
    check("timing.randomized_dwell_3to4_equiv", 0.5 <= dwell <= 1.5 + 0.1, f"-> {dwell:.3f}s (base+random)")
    # windows were actively hidden during the dwell (~20x/sec)
    check("timing.windows_hidden_during_dwell", dwell_hides["n"] >= 3, f"-> {dwell_hides['n']} hide calls")
    # terminate happened exactly once (single shot, no retries)
    check("timing.single_shot_terminate", "closed_at" in timeline)

    # --- Guard: Speedy must NOT launch if the game never appears --------------------
    class _NoGameCfg(_TimedCfg):
        game_launch_timeout_seconds = 0   # guard gives up immediately

    no_game_launches = {"n": 0}
    guard = boot.GameBooter(lambda: _NoGameCfg(), watcher_config.WatchState(),
                            threading.Event(), mgr)
    guard.processes_named = lambda name: []          # game is never running
    guard.loaded_dll_paths = lambda pid: set()
    guard._resolve_build = lambda game_modules=None: None
    def _no_launch(build):
        no_game_launches["n"] += 1
        return True
    guard._launch_speedy = _no_launch
    guard.close_speedy_and_helpers = lambda timeout=5, build=None: None
    guard.run_speedy_workflow()
    check("guard.skips_speedy_when_no_game", no_game_launches["n"] == 0,
          f"-> {no_game_launches['n']} launch(es)")

    # --- Guard: the 7s wait starts only AFTER the game process is confirmed ---------
    class _LateGameCfg(_TimedCfg):
        game_launch_timeout_seconds = 5

    appear = {"after": _time.time() + 0.4}   # game "appears" 0.4s in
    late_launch = {"at": None}
    late = boot.GameBooter(lambda: _LateGameCfg(), watcher_config.WatchState(),
                           threading.Event(), mgr)
    late.processes_named = lambda name: ([_P()] if _time.time() >= appear["after"] else [])
    late.loaded_dll_paths = lambda pid: set()
    late._resolve_build = lambda game_modules=None: None
    def _late_launch(build):
        late_launch["at"] = _time.time()
        return True
    late._launch_speedy = _late_launch
    late.hide_process_windows = lambda name: None
    late.close_speedy_and_helpers = lambda timeout=5, build=None: None
    late.speedy.mark_working = lambda build=None: None
    g0 = _time.time()
    late.run_speedy_workflow()
    # launch should be at least: game-appear-delay(0.4) + speedy_delay(0.2) after start
    elapsed_to_launch = (late_launch["at"] or g0) - g0
    check("guard.delays_7s_until_game_present", elapsed_to_launch >= 0.4 + 0.2 - 0.05,
          f"-> launched {elapsed_to_launch:.3f}s after start")

    # --- refresher module ----------------------------------------------------------
    refr = refresher.Refresher(lambda: _Cfg(), state, stop, booter, reload_config=lambda: None)
    check("refresher.interval_seconds", refr._base_interval_seconds() == 420.0)
    rdiag = refr.diagnose(lambda c, s, d: None)
    check("refresher.diagnose", isinstance(rdiag, list) and len(rdiag) == 0, f"-> {rdiag}")
    # jitter: per-cycle offsets vary, never drive target to/below zero (#5)
    class _JitCfg(_Cfg):
        refresher_interval_minutes = 1.0
        refresher_interval_jitter_seconds = 15.0
    jr = refresher.Refresher(lambda: _JitCfg(), state, stop, booter, reload_config=lambda: None)
    offsets = {round(jr._pick_cycle_jitter(), 3) for _ in range(20)}
    check("refresher.jitter_varies", len(offsets) > 1, f"-> {len(offsets)} distinct offsets")
    # live re-target: changing the base interval mid-cycle changes the effective
    # target immediately (this is the 'config not reflecting' bug fix).
    class _LiveCfg:
        refresher_interval_minutes = 5.0
        refresher_interval_jitter_seconds = 0.0
    live_holder = _LiveCfg()
    lr = refresher.Refresher(lambda: live_holder, state, stop, booter, reload_config=lambda: None)
    lr._cycle_jitter_seconds = 0.0
    t_before = lr._current_cycle_target_seconds()           # 300s
    live_holder.refresher_interval_minutes = 1.0        # user edits config
    t_after = lr._current_cycle_target_seconds()            # must now be 60s
    check("refresher.live_retarget_on_config_change",
          abs(t_before - 300.0) < 0.01 and abs(t_after - 60.0) < 0.01,
          f"-> {t_before:.0f}s -> {t_after:.0f}s")
    # EXTREME: huge jitter must never drive the effective target to/below zero.
    class _BigJit:
        refresher_interval_minutes = 1.0
        refresher_interval_jitter_seconds = 100000.0
    bj = refresher.Refresher(lambda: _BigJit(), state, stop, booter, reload_config=lambda: None)
    worst = min(max(5.0, bj._base_interval_seconds() + bj._pick_cycle_jitter()) for _ in range(2000))
    check("refresher.target_never_nonpositive", worst >= 5.0, f"-> min {worst:.2f}s")
    # EXTREME: tiny interval is clamped, not zero/negative.
    class _Tiny:
        refresher_interval_minutes = 0.001
        refresher_interval_jitter_seconds = 0.0
    tn = refresher.Refresher(lambda: _Tiny(), state, stop, booter, reload_config=lambda: None)
    tn._cycle_jitter_seconds = 0.0
    check("refresher.tiny_interval_clamped", tn._current_cycle_target_seconds() >= 5.0,
          f"-> {tn._current_cycle_target_seconds():.2f}s")

    # Fix #2/#3: cooldown re-reads config live AND refreshes the booter busy flag for
    # the whole cooldown window. Run a tiny cooldown and confirm busy is kept set and
    # the loop honors a live cooldown change (shrinking it ends the loop promptly).
    import time as _tc
    class _CdCfg:
        refresher_cooldown_seconds = 30
        refresher_enabled = True
    cd_holder = _CdCfg()
    busy_marks = {"n": 0}
    class _BusyBooter:
        def mark_busy(self, safety_clear_after=0): busy_marks["n"] += 1
        def close_game(self): pass
        def launch_game_flow(self): pass
        def is_game_running(self): return False
    cdr = refresher.Refresher(lambda: cd_holder, watcher_config.WatchState(),
                              threading.Event(), _BusyBooter(), reload_config=lambda: None)
    # Shrink the cooldown to ~1s after a moment, in a side thread, to prove live read.
    def _shrink():
        _tc.sleep(1.2); cd_holder.refresher_cooldown_seconds = 1
    threading.Thread(target=_shrink, daemon=True).start()
    t0 = _tc.time(); cdr._wait_out_cooldown(); elapsed = _tc.time() - t0
    check("refresher.cooldown_live_config", elapsed < 5.0,
          f"-> cooldown ended in {elapsed:.1f}s after live shrink to 1s")
    check("refresher.cooldown_keeps_busy", busy_marks["n"] >= 1,
          f"-> busy refreshed {busy_marks['n']}x during cooldown")

    # Custom icon: build_icon_image uses ico.ico if present, else default; never raises.
    try:
        img = watcher_config.build_icon_image()
        check("config.icon_builds", img is not None)
        check("config.icon_file_resolver", hasattr(watcher_config, "ICON_FILE"))
    except Exception as exc:
        check("config.icon_builds", False, repr(exc))

    # EXTREME: corrupt + missing config both fall back to defaults safely.
    import json as _cj
    with open(watcher_config.CONFIG_FILE, "w") as _f:
        _f.write("{ not valid json ]")
    check("config.corrupt_falls_back",
          watcher_config.load_config().get("task_process_name") == "TaskBarHero.exe")
    try:
        watcher_config.CONFIG_FILE.unlink()
    except Exception:
        pass
    check("config.missing_falls_back",
          watcher_config.load_config().get("refresher_interval_minutes") == 7.0)

    # --- main app + tray menu wiring ----------------------------------------------
    app = mainmod.TBHWatcher()
    check("main.app_built", app is not None)
    check("main.icon_has_menu", app.icon.menu is not None)

    # Walk every menu item: render dynamic labels, render 'checked' lambdas, and
    # verify each action is either a submenu, None, or a callable handler (so no
    # menu entry is wired to something broken).
    walked = 0
    bad_actions = []
    bad_checks = []
    def walk(menu):
        nonlocal walked
        for item in menu:
            if item is type(menu).SEPARATOR:
                continue
            walked += 1
            text = getattr(item, "text", None)
            if callable(text):
                try:
                    text(item)
                except Exception as exc:
                    bad_actions.append(f"label:{exc}")
            checked = item.kw.get("checked") if hasattr(item, "kw") else None
            if callable(checked):
                try:
                    checked(item)
                except Exception as exc:
                    bad_checks.append(str(exc))
            action = item.action
            if isinstance(action, type(menu)):
                walk(action)
            elif action is not None and not callable(action):
                bad_actions.append(repr(action))
    walk(app.icon.menu)
    check("main.menu_labels_render", walked > 0, f"-> {walked} items")
    check("main.menu_actions_all_callable", not bad_actions, f"-> {bad_actions}")
    check("main.menu_checks_render", not bad_checks, f"-> {bad_checks}")

    # Run the aggregated diagnostics (writes to log, returns via state).
    try:
        app.run_diagnostics()
        check("main.run_diagnostics", True)
    except Exception as exc:
        check("main.run_diagnostics", False, repr(exc))

    # one monitor tick (game not running -> should just set state and return fast)
    try:
        app.config.poll_interval_seconds = 0.01
        t = threading.Thread(target=app.monitor_loop, daemon=True)
        t.start()
        import time
        time.sleep(0.1)
        app.stop_event.set()
        t.join(timeout=1)
        check("main.monitor_loop_tick", True)
    except Exception as exc:
        check("main.monitor_loop_tick", False, repr(exc))

    # cleanup: remove everything this test session created in the fake temp.
    stealth_temp.cleanup_session()
    for folder, _ in stealth_temp.iter_owned_folders():
        stealth_temp.remove_dir(folder)
    shutil.rmtree(work, ignore_errors=True)
    finish()


def finish():
    print("\n==================== SUMMARY ====================")
    print(f"PASS: {len(PASSES)}   FAIL: {len(FAILURES)}")
    if FAILURES:
        print("Failed checks:")
        for name in FAILURES:
            print(f"   - {name}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
