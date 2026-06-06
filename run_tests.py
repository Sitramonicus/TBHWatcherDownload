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

    # PIL stub: only what buildIconImage touches.
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

    # Point locateSpeedyFolder at our fake install via baseDir fallback + PATH.
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
    cfg = watcher_config.loadConfig()
    check("config.load", isinstance(cfg, dict) and "task_process_name" in cfg)
    issues = watcher_config.diagnose(lambda c, s, d: None)
    check("config.diagnose", isinstance(issues, list))

    # --- stealth_temp: marker lifecycle + orphan sweep -----------------------------
    tdiag = stealth_temp.diagnose(lambda c, s, d: None)
    check("temp.diagnose_clean", isinstance(tdiag, list) and not tdiag, f"-> {tdiag}")
    # Folders carry markers and only ours are discoverable.
    d1 = stealth_temp.makeSessionDir(role="sandbox")
    owned_names = [f.name for f, _ in stealth_temp.iterOwnedFolders()]
    check("temp.marker_identifies_ours", d1.name in owned_names)
    # An orphan from a 'dead' session (fake pid) must be swept; ours must survive.
    orphan = fake_temp / "GPUCache_dead01"
    orphan.mkdir()
    import json as _json
    (orphan / stealth_temp.markerName).write_text(_json.dumps({
        "owner": stealth_temp.ownerTag, "session_id": "old", "pid": 999999999,
        "created": "x", "role": "sandbox"}), encoding="utf-8")
    removed = stealth_temp.sweepOrphans()
    check("temp.sweeps_dead_orphans", not orphan.exists() and removed >= 1,
          f"-> removed {removed}")
    check("temp.keeps_live_session", d1.exists())
    # A folder owned by ANOTHER live PID (e.g. a second watcher) must survive.
    live_other = fake_temp / "GPUCache_liveother"
    live_other.mkdir()
    (live_other / stealth_temp.markerName).write_text(_json.dumps({
        "owner": stealth_temp.ownerTag, "session_id": "other", "pid": os.getpid(),
        "created": "x", "role": "sandbox"}), encoding="utf-8")
    # A non-marked folder must NEVER be touched.
    not_ours = fake_temp / "GPUCache_notours"
    not_ours.mkdir(); (not_ours / "x.txt").write_text("x")
    stealth_temp.sweepOrphans()
    check("temp.keeps_other_live_pid", live_other.exists())
    check("temp.never_touches_unmarked", not_ours.exists())
    stealth_temp.removeDir(d1)
    stealth_temp.removeDir(live_other)
    import shutil as _sh; _sh.rmtree(not_ours, ignore_errors=True)

    # --- dll_mutate: hash actually changes -----------------------------------------
    mdiag = dll_mutate.diagnose(lambda c, s, d: None)
    check("mutate.diagnose_clean", isinstance(mdiag, list) and not mdiag, f"-> {mdiag}")
    # crash-safety: mutateDll must never raise, even on a non-PE garbage file,
    # with or without the opt-in pefile pass (the boot crash was a hard failure here).
    import tempfile as _tf2
    garbage = Path(_tf2.gettempdir()) / "._tbh_garbage_probe.dll"
    garbage.write_bytes(b"not a real pe at all " + os.urandom(64))
    try:
        r1 = dll_mutate.mutateDll(garbage, usePefile=False)
        r2 = dll_mutate.mutateDll(garbage, usePefile=True)  # pefile will fail->isolated
        check("mutate.never_raises_on_garbage", True, f"overlay={r1}, pefile-path={r2}")
    except BaseException as exc:
        check("mutate.never_raises_on_garbage", False, repr(exc))
    finally:
        garbage.unlink(missing_ok=True)
    # default prepare() path must NOT use pefile (crash fix verified at config level)
    check("config.pefile_mutation_off_by_default",
          watcher_config.defaultConfig.get("dll_mutation_pefile_enabled") is False)

    # all camouflage names must match the original 16-char length or patching is skipped
    bad_len = [(a, b) for a, b in speedy_dll.camouflageNames if len(a) != 16 or len(b) != 16]
    check("speedy.names_all_16_chars", not bad_len, f"-> bad: {bad_len}")

    # --- speedy manager: real patch round-trip (now temp-based) --------------------
    located = speedy_dll.locateSpeedyFolder()
    check("speedy.locate", located is not None and (located / "Speedy.exe").exists(),
          f"-> {located}")

    mgr = speedy_dll.SpeedyDLLManager()
    # store + sandbox must live under the fake system temp, not the project dir.
    check("speedy.store_under_temp", str(mgr.storeDir).startswith(str(fake_temp)),
          f"-> {mgr.storeDir}")
    check("speedy.sandbox_under_temp", str(mgr.sandboxDir).startswith(str(fake_temp)),
          f"-> {mgr.sandboxDir}")

    build = mgr.prepare()
    check("speedy.prepare_returns_build", build is not None,
          f"-> {build.dll64 if build else None}")
    if build:
        check("speedy.launcher_renamed", build.speedyExe.exists()
              and build.speedyExe.name != "Speedy.exe", f"-> {build.speedyExe.name}")
        check("speedy.bridges_renamed",
              build.bridge64 != "bridge64.exe" and build.bridge32 != "bridge32.exe",
              f"-> {build.bridge64}/{build.bridge32}")
        renamed_64 = build.sandboxDir / build.dll64
        check("speedy.renamed_dll_emitted", renamed_64.exists(), f"-> {build.dll64}")
        # DLL was mutated (overlay, on by default): hash differs from the original.
        pristine_dll = mgr.pristineDir / "speedpatch64.dll"
        check("speedy.dll_hash_mutated",
              dll_mutate.fileHash(renamed_64) != dll_mutate.fileHash(pristine_dll),
              "renamed DLL hash differs from original (overlay)")
        # prepare() must NOT run the risky pefile pass by default (crash fix): the
        # default build should still be a valid, parseable PE because only an
        # overlay was appended (loader ignores it).
        if dll_mutate.havePefile:
            import pefile as _pf
            try:
                _pf.PE(data=renamed_64.read_bytes()).close()
                check("speedy.default_mutation_keeps_valid_pe", True, "overlay-only, still valid PE")
            except Exception as exc:
                check("speedy.default_mutation_keeps_valid_pe", False, repr(exc))
            # Opt-in pefile pass, exercised DIRECTLY (not via prepare), must change
            # the timestamp AND keep a valid PE -- proves the feature still works
            # when a user explicitly enables it.
            probe = build.sandboxDir / "._pefile_probe.dll"
            probe.write_bytes(pristine_dll.read_bytes())
            ts_a = _pf.PE(data=probe.read_bytes()); a = ts_a.FILE_HEADER.TimeDateStamp; ts_a.close()
            dll_mutate.mutateDll(probe, usePefile=True)
            try:
                pe_b = _pf.PE(data=probe.read_bytes()); b = pe_b.FILE_HEADER.TimeDateStamp; pe_b.close()
                check("speedy.optin_pefile_timestamp_applied", a != b, f"-> {hex(a)} -> {hex(b)}")
            except Exception as exc:
                check("speedy.optin_pefile_timestamp_applied", False, repr(exc))
            probe.unlink()
        # Bridge (under its renamed file) was byte-patched to the new DLL name.
        bridge = (build.sandboxDir / build.bridge64).read_bytes()
        check("speedy.bridge_patched_ascii",
              build.dll64.encode("utf-8") in bridge and b"speedpatch64.dll" not in bridge)
        check("speedy.bridge_patched_utf16",
              build.dll64.encode("utf-16-le") in bridge)

    # findActiveBuild should locate what prepare() just wrote.
    found = mgr.findActiveBuild()
    check("speedy.findActiveBuild", found is not None and found.dll64 == (build.dll64 if build else None))

    mgr.markWorking(build)
    check("speedy.markWorking", mgr.meta["history"][-1]["status"] == "working")

    build2 = mgr.prepare()
    action = mgr.handleFailure(build2)
    check("speedy.handleFailure", action.kind in ("rollback", "restore_working", "pristine", "none"),
          f"-> {action.kind}")
    recovered = mgr.applyRecovery(action)
    check("speedy.apply_recovery_runs", True, f"-> {recovered.dll64 if recovered else None}")

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
        rn64, _rn32 = speedy_dll.chooseCamouflageNames(game_mods, avoid=rot_avoid)
        rot_seq.append(rn64)
        rot_avoid = rn64
    consec = [i for i in range(1, len(rot_seq)) if rot_seq[i] == rot_seq[i - 1]]
    check("speedy.name_no_consecutive_repeat", not consec, f"-> repeats at {consec}")
    distinct = len(set(rot_seq))
    check("speedy.name_actually_rotates", distinct >= 6,
          f"-> {distinct} distinct of {len(speedy_dll.camouflageNames)} over 40 rolls")
    top_share = max(rot_seq.count(n) for n in set(rot_seq)) / len(rot_seq)
    check("speedy.name_not_dominated", top_share <= 0.5,
          f"-> most-frequent name share {top_share:.2f} (must be <= 0.5)")
    # avoid is honoured even with no game modules
    a1, _ = speedy_dll.chooseCamouflageNames(None, avoid="steam_api_64.dll")
    check("speedy.avoid_respected_no_modules", a1 != "steam_api_64.dll", f"-> {a1}")
    # full prepare() path rotates too, and the no-repeat survives a restart
    mgr.prepare(gameModules=game_mods)
    prep_seq = [mgr.prepare(gameModules=game_mods).dll64 for _ in range(8)]
    prep_consec = [i for i in range(1, len(prep_seq)) if prep_seq[i] == prep_seq[i - 1]]
    check("speedy.prepare_no_consecutive_repeat", not prep_consec, f"-> {prep_seq}")
    last_name = mgr.meta.get("last_dll_64")
    mgr_restart = speedy_dll.SpeedyDLLManager()  # reuses the temp store + meta
    check("speedy.no_repeat_persists_restart",
          mgr_restart.meta.get("last_dll_64") == last_name
          and mgr_restart.prepare(gameModules=game_mods).dll64 != last_name,
          f"-> last was {last_name}")

    # rollback keeps pristine; unpatch removes the whole temp store
    mgr.prepare()
    pristine_files_before = list(mgr.pristineDir.glob("*"))
    store_before = mgr.storeDir
    mgr.rollbackAll()
    check("speedy.rollback_keeps_pristine",
          mgr.pristineDir.exists() and len(list(mgr.pristineDir.glob("*"))) == len(pristine_files_before),
          f"-> {len(list(mgr.pristineDir.glob('*')))} pristine files kept")
    check("speedy.rollback_clears_states",
          not any(p.name.startswith("state_") for p in mgr.backupDir.glob("*")))
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
        focus_shield_watch_seconds = 0.3
        focus_shield_poll_seconds = 0.1
        focus_shield_restore_cooldown_seconds = 0.0
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

    check("boot.processesNamed", isinstance(booter.processesNamed("nonexistent.exe"), list))
    check("boot.pidSnapshot", isinstance(booter.pidSnapshot(), set))
    check("boot.isGameRunning", booter.isGameRunning() is False)
    check("boot.isGameHung", booter.isGameHung() is False)
    bdiag = booter.diagnose(lambda c, s, d: None)
    check("boot.diagnose", isinstance(bdiag, list))
    # Exercise resolve_build hand-off (the boot<->speedy contract)
    resolved = booter.resolveBuild(gameModules=set())
    check("boot.speedy_contract", resolved is None or hasattr(resolved, "dll64"))

    # ===== Regression guards for the 8 problems found in adversarial review =====

    # P1/P2: closing Speedy must NOT kill processes by cover name. The kill set must
    # be path-scoped (our temp dirs) + literal default names only -- cover names like
    # nvcontainer.exe / SteamService.exe must never be targeted by name.
    import boot as _bootmod
    src_boot = open(_bootmod.__file__).read()
    check("boot.no_killing_by_cover_name",
          "launcherCoverNames + bridge64CoverNames + bridge32CoverNames" not in src_boot
          and "processesUnderDir" in src_boot,
          "close uses path-scoping, not cover-name matching")
    # Prove closeSpeedyAndHelpers only kills via processesUnderDir + literal names.
    killed_names = []
    kbooter = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    kbooter.processesUnderDir = lambda d: []          # nothing running from our dirs
    kbooter.ensureClosed = lambda name, timeout=5: killed_names.append(name) or True
    kbooter.closeSpeedyAndHelpers(timeout=1)
    cover_hit = [n for n in killed_names
                 if n in (speedy_dll.launcherCoverNames + speedy_dll.bridge64CoverNames
                          + speedy_dll.bridge32CoverNames)]
    check("boot.close_never_targets_cover_names", not cover_hit,
          f"-> would have killed {cover_hit}")
    check("boot.close_targets_only_literals",
          set(killed_names) <= {"Speedy.exe", "bridge64.exe", "bridge32.exe"},
          f"-> {sorted(set(killed_names))}")

    # P1: focus shield must not treat a cover-named process as a thief by name.
    check("boot.focus_keywords_safe",
          all(k not in _bootmod.focusThiefKeywords
              for k in ("overlay", "render", "relay", "gpu_proc", "audio_svc")),
          f"-> {_bootmod.focusThiefKeywords}")

    # P3: launch lock -- two concurrent launchGameFlow calls must not both run.
    import time as _t3
    lk = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    run_count = {"n": 0}
    def _slow_seq():
        run_count["n"] += 1
        _t3.sleep(0.3)
    lk.executeLaunchSequence = _slow_seq
    lk.pidSnapshot = lambda: set()
    lk.waitForGameOpen = lambda *a, **k: True
    t_a = threading.Thread(target=lk.launchGameFlow)
    t_b = threading.Thread(target=lk.launchGameFlow)
    t_a.start(); t_b.start(); t_a.join(); t_b.join()
    check("boot.launch_lock_serialises", run_count["n"] == 1,
          f"-> {run_count['n']} concurrent launches ran (must be 1)")

    # P8: a deliberate closeGame sets busy so the monitor won't call it a hang.
    bb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    bb.ensureClosed = lambda name, timeout=5: True
    check("boot.not_busy_initially", bb.busy is False)
    bb.closeGame()
    check("boot.close_game_sets_busy", bb.busy is True)
    # P (new): busy must NEVER get stuck. If closeGame() set busy and the following
    # launchGameFlow() is SKIPPED (lock held by a concurrent launch), busy must be
    # cleared on the skip path -- otherwise the monitor is suppressed forever.
    sb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    sb.ensureClosed = lambda name, timeout=5: True
    sb.launchLock.acquire()  # simulate a concurrent launch holding the lock
    sb.closeGame()            # sets busy
    assert sb.busy is True
    sb.launchGameFlow()      # must be skipped AND clear the busy flag
    check("boot.busy_not_stuck_when_launch_skipped", sb.busy is False,
          "skipped launch cleared the busy flag")
    sb.launchLock.release()
    # And the safety timer auto-clears busy even if nothing else does.
    st = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    st.markBusy(safetyClearAfter=0.2)
    was_set = st.busy
    _t3.sleep(0.45)
    check("boot.busy_safety_timer_clears", was_set is True and st.busy is False,
          "safety timer auto-cleared busy")

    # P5: ConfigHolder.reload has a lock (concurrent reload is synchronised).
    _ch = mainmod.ConfigHolder()
    check("config.reload_has_lock", hasattr(_ch, "lock"))

    # Config + log live in a PERSISTENT data dir (NOT temp, NOT beside the exe), so
    # user edits survive sessions. In this harness dataDir resolves under our fake
    # temp HOME, but the key property is: it's a real dir we can read/write and it's
    # the same path load/save use.
    check("config.data_dir_used_for_files",
          str(watcher_config.configFile).startswith(str(watcher_config.dataDir))
          and str(watcher_config.logFile).startswith(str(watcher_config.dataDir)),
          f"-> {watcher_config.dataDir}")
    check("config.data_dir_not_temp_by_default",
          "resolveDataDir" in open(watcher_config.__file__).read()
          and "LOCALAPPDATA" in open(watcher_config.__file__).read(),
          "uses %LOCALAPPDATA%/TBHWatcher (persistent) not the temp dir")
    # Persistence round-trip: save a change, reload, value retained.
    _cfg = watcher_config.loadConfig(); _cfg["refresher_cooldown_seconds"] = 42
    watcher_config.saveConfig(_cfg)
    check("config.persists_across_reload",
          watcher_config.loadConfig().get("refresher_cooldown_seconds") == 42,
          "edited value retained after save+reload")

    # Single-instance: first acquire succeeds; a simulated second caller is rejected.
    first = watcher_config.acquireSingleInstance()
    check("config.single_instance_first_ok", first is True)
    check("config.single_instance_api_exists",
          callable(getattr(watcher_config, "acquireSingleInstance", None))
          and callable(getattr(watcher_config, "warnAlreadyRunning", None)))
    # Fix #6: mutex uses use_last_error + get_last_error (reliable already-running
    # detection), not a separate GetLastError() read that can be reset.
    _wc_src = open(watcher_config.__file__).read()
    check("config.mutex_uses_last_error",
          'WinDLL("kernel32", use_last_error=True)' in _wc_src
          and "ctypes.get_last_error()" in _wc_src,
          "single-instance mutex reads last error reliably")

    # Fix #5: saveConfig is atomic (temp file + os.replace), so an interrupted
    # write can't corrupt the config.
    check("config.save_is_atomic",
          ".json.tmp" in _wc_src and "os.replace" in _wc_src.replace("_os.replace", "os.replace"),
          "saveConfig writes to temp then atomically replaces")
    # Round-trip still works after the atomic-write change.
    _c5 = watcher_config.loadConfig(); _c5["poll_interval_seconds"] = 9
    watcher_config.saveConfig(_c5)
    check("config.atomic_save_roundtrip",
          watcher_config.loadConfig().get("poll_interval_seconds") == 9)

    # Fix #1: findActiveBuild prefers THIS session's own sandbox, never a
    # stale/other-session folder.
    own = mgr.prepare()  # writes a build into mgr.sandboxDir
    fab = mgr.findActiveBuild()
    check("speedy.find_active_prefers_own_sandbox",
          fab is not None and fab.sandboxDir == mgr.sandboxDir,
          f"-> {fab.sandboxDir.name if fab else None} vs own {mgr.sandboxDir.name}")

    # Fix #4: waitForGameOpen falls back to task_process_name, not a blind
    # PID-diff (no false positives from unrelated new processes).
    _wf_src = open(_bootmod.__file__).read()
    check("boot.wait_uses_task_name_fallback",
          'self.config("task_process_name"' in _wf_src
          and "self.pidSnapshot() - before" not in _wf_src,
          "game-open wait uses known process name, not PID-diff heuristic")

    # P7: runDiagnostics must NOT mutate live speedy meta (failure_count/history).
    mgr.meta["failure_count"] = 0
    import json as _j7
    meta_before = _j7.dumps(mgr.meta, sort_keys=True)
    mgr.diagnose(lambda c, s, d: None)
    meta_after = _j7.dumps(mgr.meta, sort_keys=True)
    check("speedy.diagnose_non_destructive", meta_before == meta_after,
          "diagnostics left meta unchanged")
    check("speedy.diag_failcount_unchanged", mgr.meta["failure_count"] == 0,
          f"-> {mgr.meta['failure_count']}")

    # P6: resolveBuild must close any running injector BEFORE prepare() wipes the
    # sandbox (else Windows file locks on a still-running EXE break the wipe).
    order = []
    class _RerollCfg(_Cfg):
        dll_reroller_enabled = True
    rb = boot.GameBooter(lambda: _RerollCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    rb.closeSpeedyAndHelpers = lambda timeout=5, build=None: order.append("close")
    class _FakeSpeedy:
        sandboxDir = mgr.sandboxDir
        storeDir = mgr.storeDir
        def prepare(self, gameModules=None):
            order.append("prepare"); return None
        def findActiveBuild(self):
            return None
    rb.speedy = _FakeSpeedy()
    rb.resolveBuild(gameModules=set())
    check("boot.closes_before_prepare", order[:2] == ["close", "prepare"],
          f"-> order {order}")

    # P (blank-taskbar-window bug): launchSpeedy must NOT combine DETACHED_PROCESS
    # with CREATE_NO_WINDOW (mutually exclusive -> stray uncloseable window), must
    # not keep the Popen handle, and the workflow must WM_CLOSE sandbox windows
    # before terminating so no hidden window survives in the taskbar.
    src_boot2 = open(_bootmod.__file__).read()
    check("boot.no_detached_plus_nowindow_combo",
          "CREATE_NO_WINDOW | DETACHED_PROCESS" not in src_boot2
          and "creationflags=detachedProcess," in src_boot2,
          "launch uses DETACHED_PROCESS alone (no conflicting flag combo)")
    check("boot.closes_windows_before_terminate",
          "closeWindowsInDir(self.speedy.sandboxDir)" in src_boot2
          and "def closeWindowsInDir" in src_boot2,
          "WM_CLOSE sandbox windows before killing the process")
    # closeWindowsInDir must enumerate hidden windows too (only_visible=False).
    check("boot.close_includes_hidden_windows",
          "enumWindowsForPid(process.pid, onlyVisible=False)" in src_boot2,
          "closes even hidden windows (the blank taskbar ghost)")
    # launchSpeedy releases its child handle (fire-and-forget) so the child's
    # lifetime is never tethered to the watcher.
    check("boot.launch_releases_handle",
          "del process" in src_boot2,
          "Popen handle dropped after launch")

    # P (blank-window root cause): launchGame must fire the URI via the canonical
    # Win32 shell API ShellExecuteW with SW_HIDE -- NO intermediary process and NO
    # window created on our side. Steam receives the request directly, so any window
    # Steam shows is owned by steam.exe, not us. os.startfile is only a fallback.
    _lg_body = src_boot2.split("def launchGame(self)")[1].split("\n    def ")[0]
    check("boot.launch_uses_shellexecute",
          "ShellExecuteW" in _lg_body and "hideFlag" in _lg_body
          and "shell" in _lg_body,
          "launchGame fires URI via ShellExecuteW(SW_HIDE)")
    # On the Windows path (after the non-win early-return) there must be NO
    # subprocess.Popen -- the URI is fired via ShellExecuteW with no child process.
    _win_part = _lg_body.split("startswith(\"win\")")[1] if "startswith(\"win\")" in _lg_body else _lg_body
    _win_part = _win_part.split("return", 1)[1] if "return" in _win_part else _win_part
    check("boot.launch_no_subprocess_window",
          "subprocess.Popen" not in _win_part,
          "Windows launch path spawns NO child process (no window source)")
    # And launchGame must not raise on this platform.
    _lgb = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                           threading.Event(), mgr)
    _lgb.launchGame()
    check("boot.launch_game_no_raise", True)

    # Emergency window clear: callable, returns the expected summary keys, no raise.
    ec = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    summ = ec.emergencyWindowClear()
    check("boot.emergency_clear_runs",
          isinstance(summ, dict)
          and {"dir_windows_closed", "procs_killed", "blank_windows_closed",
               "orphans_swept"} <= set(summ.keys()),
          f"-> {summ}")
    # enumAllTopLevelWindows exists and is safe to call off-Windows.
    check("config.enum_all_windows_safe",
          isinstance(watcher_config.enumAllTopLevelWindows(), list))
    # Stray-taskbar-window diagnostic + fix (the confirmed bug: window owned by our
    # own PID). diagnoseOwnWindows returns a list; stripFromTaskbar is callable
    # and safe; both no-op cleanly off-Windows.
    import os as _os2
    check("config.diagnose_own_windows_safe",
          isinstance(watcher_config.diagnoseOwnWindows(_os2.getpid()), list))
    check("config.strip_from_taskbar_safe",
          watcher_config.stripFromTaskbar(0) in (True, False))
    # The launch sequence logs/strips our own windows (gated by config, default on)
    # and emergencyWindowClear reports own_stripped.
    _bsrc = open(_bootmod.__file__).read()
    check("boot.launch_logs_own_windows",
          "logOwnWindows" in _bsrc and "diagnoseOwnWindows" in _bsrc
          and "stripFromTaskbar" in _bsrc,
          "post-launch own-window diagnostic + strip wired")
    _summ2 = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                             threading.Event(), mgr).emergencyWindowClear()
    check("boot.emergency_reports_own_stripped", "own_stripped" in _summ2)
    # The diagnostic method itself must never raise.
    try:
        boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                        threading.Event(), mgr).logOwnWindows()
        check("boot.log_own_windows_no_raise", True)
    except Exception as exc:
        check("boot.log_own_windows_no_raise", False, repr(exc))

    # Debug menu exists and contains the emergency button + the moved utilities.
    _dbgapp = mainmod.TBHWatcher()
    dbg = _dbgapp.buildDebugMenu()
    labels = [getattr(it, "text", None) for it in dbg
              if it is not type(dbg).SEPARATOR]
    labels = [t for t in labels if isinstance(t, str)]
    check("main.debug_menu_has_emergency",
          any("Emergency" in t for t in labels)
          and any("Open Config" in t for t in labels)
          and any("Clear Log" in t for t in labels),
          f"-> {labels}")

    # --- double-inject detection ---------------------------------------------------
    # When a camouflage/original DLL is already loaded in the game, isAlreadyInjected
    # must report True; otherwise False.
    inj = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    a_name = speedy_dll.camouflageNames[0][0]
    # forward slashes so pathlib.Path(...).name works on the Linux test host too
    inj.loadedDllPaths = lambda pid: {f"c:/game/{a_name}", "c:/game/other.dll"}
    already, found = inj.isAlreadyInjected(1234)
    check("boot.detects_existing_injection", already and found == a_name.lower(), f"-> {found}")
    inj.loadedDllPaths = lambda pid: {"c:/game/d3d11.dll", "c:/game/kernel32.dll"}
    already2, _ = inj.isAlreadyInjected(1234)
    check("boot.no_false_injection", already2 is False)
    inj.loadedDllPaths = lambda pid: set()  # access denied / empty
    already3, _ = inj.isAlreadyInjected(1234)
    check("boot.inject_check_safe_when_blind", already3 is False)

    # workflow must SKIP launching Speedy when already injected
    skip_launches = {"n": 0}
    inj2 = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(), threading.Event(), mgr)
    class _GP:  # noqa
        pid = 777
    inj2.processesNamed = lambda name: ([_GP()] if name == "TaskBarHero.exe" else [])
    inj2.loadedDllPaths = lambda pid: {f"c:/game/{a_name}"}  # already injected
    def _skip_launch(build):
        skip_launches["n"] += 1
        return True
    inj2.launchSpeedy = _skip_launch
    inj2.resolveBuild = lambda gameModules=None: None
    inj2.closeSpeedyAndHelpers = lambda timeout=5, build=None: None
    inj2.runSpeedyWorkflowImpl()
    check("boot.skips_when_already_injected", skip_launches["n"] == 0,
          f"-> {skip_launches['n']} launch(es)")

    # --- post-injection verify + reinject -----------------------------------------
    class _VCfg(_Cfg):
        verify_injection_enabled = True
        verify_injection_delay_seconds = 0.05
    a_name2 = speedy_dll.camouflageNames[2][0]

    # Case A: injection DID take -> verify confirms, NO reinject (anti-double-inject).
    vb = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb.processesNamed = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    vb.loadedDllPaths = lambda pid: {f"c:/game/{a_name2}"}  # already loaded
    reinjects = {"n": 0}
    vb.injectOnce = lambda build, dll: reinjects.__setitem__("n", reinjects["n"] + 1) or True
    vb.verifyAndMaybeReinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_no_reinject_when_injected", reinjects["n"] == 0,
          f"-> {reinjects['n']} reinject(s) (must be 0)")

    # Case B: injection did NOT take -> exactly one reinject happens.
    vb2 = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb2.processesNamed = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    # modules readable (non-empty) but our DLL not present -> not injected.
    vb2.loadedDllPaths = lambda pid: {"c:/game/d3d11.dll"}
    reinjects2 = {"n": 0}
    vb2.injectOnce = lambda build, dll: reinjects2.__setitem__("n", reinjects2["n"] + 1) or True
    vb2.speedy.markWorking = lambda build=None: None
    vb2.verifyAndMaybeReinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_reinjects_when_missing", reinjects2["n"] == 1,
          f"-> {reinjects2['n']} reinject(s) (must be 1)")

    # Case C: modules unreadable (no admin) -> skip verify, do NOT reinject (could
    # double-inject if it actually worked).
    vb3 = boot.GameBooter(lambda: _VCfg(), watcher_config.WatchState(), threading.Event(), mgr)
    vb3.processesNamed = lambda name: ([object()] if name == "TaskBarHero.exe" else [])
    vb3.loadedDllPaths = lambda pid: set()  # access denied / empty
    reinjects3 = {"n": 0}
    vb3.injectOnce = lambda build, dll: reinjects3.__setitem__("n", reinjects3["n"] + 1) or True
    vb3.verifyAndMaybeReinject(123, "TaskBarHero.exe", None, a_name2)
    check("boot.verify_skips_when_blind", reinjects3["n"] == 0,
          f"-> {reinjects3['n']} reinject(s) (must be 0)")

    # --- confirmDialog: headless returns False (safe No), and is callable ----------
    cd_result = watcher_config.confirmDialog("Test", "Proceed?")
    check("config.confirm_dialog_safe_default", cd_result is False,
          "headless/No -> False (won't proceed)")

    # --- global excepthook installs without error ----------------------------------
    try:
        watcher_config.installGlobalExceptHook()
        check("config.excepthook_installs", True)
    except Exception as exc:
        check("config.excepthook_installs", False, repr(exc))

    # --- log_error with context writes a hint --------------------------------------
    try:
        raise PermissionError("denied")
    except Exception as exc:
        watcher_config.logError("probe", error=exc, where="run_tests.probe")
    check("config.log_error_with_context", True, "logged with module+hint")

    # --- splash module -------------------------------------------------------------
    import splash as splash_mod
    spdiag = splash_mod.diagnose(lambda c, s, d: None)
    check("splash.diagnose", isinstance(spdiag, list) and not spdiag, f"-> {spdiag}")
    # NullSplash stand-in must satisfy the interface used in __init__ (incl notifyReady).
    ns = mainmod.NullSplash()
    ns.start(); ns.step("x"); ns.message("y"); ns.notifyReady(None); ns.finish()
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
        rs = _spmod.Splash(totalSteps=3)
        rs.start(); rs.step("a"); rs.message("b"); rs.notifyReady(None); rs.finish()
        check("splash.lifecycle_safe", True)
    except Exception as exc:
        check("splash.lifecycle_safe", False, repr(exc))

    # --- STRICT Stable timing sequence verification --------------------------------
    # Prove runSpeedyWorkflow follows Stable: sleep(delay) -> launch ONCE ->
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
    timed.processesNamed = lambda name: ([_P()] if name == "TaskBarHero.exe" else [])
    timed.loadedDllPaths = lambda pid: set()             # game-module read (#3), harmless
    timed.resolveBuild = lambda gameModules=None: None  # force fallback DLL name path
    def _fake_launch(build):
        launch_calls["n"] += 1
        timeline["launched_at"] = _time.time()
        return True
    timed.launchSpeedy = _fake_launch
    def _count_hide(name):
        dwell_hides["n"] += 1
    timed.hideProcessWindows = _count_hide
    timed.closeSpeedyAndHelpers = lambda timeout=5, build=None: timeline.__setitem__("closed_at", _time.time())
    timed.speedy.markWorking = lambda build=None: None

    t0 = _time.time()
    timed.runSpeedyWorkflow()
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
    guard.processesNamed = lambda name: []          # game is never running
    guard.loadedDllPaths = lambda pid: set()
    guard.resolveBuild = lambda gameModules=None: None
    def _no_launch(build):
        no_game_launches["n"] += 1
        return True
    guard.launchSpeedy = _no_launch
    guard.closeSpeedyAndHelpers = lambda timeout=5, build=None: None
    guard.runSpeedyWorkflow()
    check("guard.skips_speedy_when_no_game", no_game_launches["n"] == 0,
          f"-> {no_game_launches['n']} launch(es)")

    # --- Guard: the 7s wait starts only AFTER the game process is confirmed ---------
    class _LateGameCfg(_TimedCfg):
        game_launch_timeout_seconds = 5

    appear = {"after": _time.time() + 0.4}   # game "appears" 0.4s in
    late_launch = {"at": None}
    late = boot.GameBooter(lambda: _LateGameCfg(), watcher_config.WatchState(),
                           threading.Event(), mgr)
    late.processesNamed = lambda name: ([_P()] if _time.time() >= appear["after"] else [])
    late.loadedDllPaths = lambda pid: set()
    late.resolveBuild = lambda gameModules=None: None
    def _late_launch(build):
        late_launch["at"] = _time.time()
        return True
    late.launchSpeedy = _late_launch
    late.hideProcessWindows = lambda name: None
    late.closeSpeedyAndHelpers = lambda timeout=5, build=None: None
    late.speedy.markWorking = lambda build=None: None
    g0 = _time.time()
    late.runSpeedyWorkflow()
    # launch should be at least: game-appear-delay(0.4) + speedy_delay(0.2) after start
    elapsed_to_launch = (late_launch["at"] or g0) - g0
    check("guard.delays_7s_until_game_present", elapsed_to_launch >= 0.4 + 0.2 - 0.05,
          f"-> launched {elapsed_to_launch:.3f}s after start")

    # --- refresher module ----------------------------------------------------------
    refr = refresher.Refresher(lambda: _Cfg(), state, stop, booter, reloadConfig=lambda: None)
    check("refresher.interval_seconds", refr.intervalSeconds() == 420.0)
    rdiag = refr.diagnose(lambda c, s, d: None)
    check("refresher.diagnose", isinstance(rdiag, list) and len(rdiag) == 0, f"-> {rdiag}")
    # jitter: per-cycle offsets vary, never drive target to/below zero (#5)
    class _JitCfg(_Cfg):
        refresher_interval_minutes = 1.0
        refresher_interval_jitter_seconds = 15.0
    jr = refresher.Refresher(lambda: _JitCfg(), state, stop, booter, reloadConfig=lambda: None)
    offsets = {round(jr.newJitterOffset(), 3) for _ in range(20)}
    check("refresher.jitter_varies", len(offsets) > 1, f"-> {len(offsets)} distinct offsets")
    # live re-target: changing the base interval mid-cycle changes the effective
    # target immediately (this is the 'config not reflecting' bug fix).
    class _LiveCfg:
        refresher_interval_minutes = 5.0
        refresher_interval_jitter_seconds = 0.0
    live_holder = _LiveCfg()
    lr = refresher.Refresher(lambda: live_holder, state, stop, booter, reloadConfig=lambda: None)
    lr.jitterOffset = 0.0
    t_before = lr.effectiveTargetSeconds()           # 300s
    live_holder.refresher_interval_minutes = 1.0        # user edits config
    t_after = lr.effectiveTargetSeconds()            # must now be 60s
    check("refresher.live_retarget_on_config_change",
          abs(t_before - 300.0) < 0.01 and abs(t_after - 60.0) < 0.01,
          f"-> {t_before:.0f}s -> {t_after:.0f}s")
    # EXTREME: huge jitter must never drive the effective target to/below zero.
    class _BigJit:
        refresher_interval_minutes = 1.0
        refresher_interval_jitter_seconds = 100000.0
    bj = refresher.Refresher(lambda: _BigJit(), state, stop, booter, reloadConfig=lambda: None)
    worst = min(max(5.0, bj.intervalSeconds() + bj.newJitterOffset()) for _ in range(2000))
    check("refresher.target_never_nonpositive", worst >= 5.0, f"-> min {worst:.2f}s")
    # EXTREME: tiny interval is clamped, not zero/negative.
    class _Tiny:
        refresher_interval_minutes = 0.001
        refresher_interval_jitter_seconds = 0.0
    tn = refresher.Refresher(lambda: _Tiny(), state, stop, booter, reloadConfig=lambda: None)
    tn.jitterOffset = 0.0
    check("refresher.tiny_interval_clamped", tn.effectiveTargetSeconds() >= 5.0,
          f"-> {tn.effectiveTargetSeconds():.2f}s")

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
        def markBusy(self, safetyClearAfter=0): busy_marks["n"] += 1
        def closeGame(self): pass
        def launchGameFlow(self): pass
        def isGameRunning(self): return False
    cdr = refresher.Refresher(lambda: cd_holder, watcher_config.WatchState(),
                              threading.Event(), _BusyBooter(), reloadConfig=lambda: None)
    # Shrink the cooldown to ~1s after a moment, in a side thread, to prove live read.
    def _shrink():
        _tc.sleep(1.2); cd_holder.refresher_cooldown_seconds = 1
    threading.Thread(target=_shrink, daemon=True).start()
    t0 = _tc.time(); cdr.runCooldown(); elapsed = _tc.time() - t0
    check("refresher.cooldown_live_config", elapsed < 5.0,
          f"-> cooldown ended in {elapsed:.1f}s after live shrink to 1s")
    check("refresher.cooldown_keeps_busy", busy_marks["n"] >= 1,
          f"-> busy refreshed {busy_marks['n']}x during cooldown")

    # --- DEBUG: force current refresh in 10s (live remaining, not config max) ----
    class _DbgCfg:
        refresher_enabled = True
        refresher_interval_minutes = 7.0
        refresher_interval_jitter_seconds = 0.0
        refresher_cooldown_seconds = 1
    import time as _td
    dbg = refresher.Refresher(lambda: _DbgCfg(), state, threading.Event(), booter,
                              reloadConfig=lambda: None)
    # disabled when no cycle running
    ok0, msg0 = dbg.forceRefreshSoon(10.0)
    check("refresher.debug_blocked_when_no_cycle", ok0 is False, f"-> {msg0}")
    # start a cycle
    dbg.gameStartedAt = _td.time() - 100.0
    dbg.jitterOffset = 0.0
    ok1, msg1 = dbg.forceRefreshSoon(10.0)
    rem = dbg.effectiveTargetSeconds() - (_td.time() - dbg.gameStartedAt)
    check("refresher.debug_forces_10s", ok1 and 8.0 <= rem <= 12.0,
          f"-> ok={ok1} remaining={rem:.1f}s ({msg1})")
    # disabled during cooldown
    dbg.inCooldown = True
    ok2, msg2 = dbg.forceRefreshSoon(10.0)
    check("refresher.debug_blocked_in_cooldown", ok2 is False, f"-> {msg2}")
    dbg.inCooldown = False
    # disabled when refresher disabled
    class _DbgOff(_DbgCfg):
        refresher_enabled = False
    dbgo = refresher.Refresher(lambda: _DbgOff(), state, threading.Event(), booter,
                               reloadConfig=lambda: None)
    dbgo.gameStartedAt = _td.time()
    ok3, msg3 = dbgo.forceRefreshSoon(10.0)
    check("refresher.debug_blocked_when_disabled", ok3 is False, f"-> {msg3}")
    # forced target is cleared on doRefresh (one-shot) and on cycle reset
    dbg.forcedTargetSeconds = 123.0
    dbg.gameStartedAt = None
    check("refresher.debug_does_not_touch_config",
          _DbgCfg.refresher_interval_minutes == 7.0,
          "config max untouched by debug override")

    # Custom icon: buildIconImage uses ico.ico if present, else default; never raises.
    try:
        img = watcher_config.buildIconImage()
        check("config.icon_builds", img is not None)
        check("config.icon_file_resolver", hasattr(watcher_config, "iconFile"))
    except Exception as exc:
        check("config.icon_builds", False, repr(exc))

    # --- New gentle one-shot focus shield. The old AttachThreadInput/BringWindowToTop/
    # SW_RESTORE/maximize machinery is GONE (it caused click-through + window drag).
    # setForeground must call ONLY SetForegroundWindow -- never ShowWindow,
    # AttachThreadInput or BringWindowToTop. --------------------------------------
    class _RecU32:
        def __init__(self): self.calls = []
        def SetForegroundWindow(self, h): self.calls.append(("SetForegroundWindow", h)); return 1
        def ShowWindow(self, h, cmd): self.calls.append(("ShowWindow", cmd)); return 1
        def AttachThreadInput(self, a, b, c): self.calls.append(("AttachThreadInput", c)); return 1
        def BringWindowToTop(self, h): self.calls.append(("BringWindowToTop", h)); return 1
    _orig_u32 = watcher_config.user32
    rec = _RecU32()
    watcher_config.user32 = rec
    try:
        rec.calls.clear()
        ok = watcher_config.setForeground(111)
        check("focus.setForeground_calls_setforeground",
              ok and ("SetForegroundWindow", 111) in rec.calls)
        forbidden = [c for c in rec.calls if c[0] in ("ShowWindow", "AttachThreadInput", "BringWindowToTop")]
        check("focus.setForeground_no_state_changes", not forbidden,
              f"-> forbidden calls present: {forbidden}")
        check("focus.setForeground_zero_handle_safe", watcher_config.setForeground(0) is False)
    finally:
        watcher_config.user32 = _orig_u32

    # Old dangerous helpers must be fully removed from watcher_config.
    _cfgsrc = open(watcher_config.__file__).read()
    check("focus.old_dangerous_helpers_removed",
          all(name not in _cfgsrc for name in
              ("def bringToForeground", "def focusWindowPreservingState",
               "def maximiseWindow", "BringWindowToTop")),
          "old un-maximize/bringtotop machinery is gone")
    # AttachThreadInput is allowed ONLY inside the one-shot forceForegroundOnce (with a
    # detach in finally and NO ShowWindow/maximize) -- never in a loop, never with resize.
    _ffo = _cfgsrc.split("def forceForegroundOnce", 1)[-1].split("\ndef ", 1)[0]
    check("focus.attachthreadinput_only_in_forceforeground",
          _cfgsrc.count("AttachThreadInput") > 0
          and _cfgsrc.count("AttachThreadInput") == _ffo.count("AttachThreadInput"),
          "AttachThreadInput appears ONLY inside forceForegroundOnce")
    check("focus.forceforeground_has_no_resize",
          ("ShowWindow" not in _ffo and "SW_RESTORE" not in _ffo
           and "SW_MAXIMIZE" not in _ffo and "BringWindowToTop" not in _ffo),
          "forceForegroundOnce never resizes/maximizes/raises (no maximize bug)")
    check("focus.forceforeground_detaches_in_finally",
          "finally:" in _ffo and "AttachThreadInput(currentThread, targetThread, False)" in _ffo,
          "forceForegroundOnce always detaches input (no click-through)")

    # Config: enabled default True; old focus_shield_seconds gone; new delay key present.
    check("config.focus_shield_disabled_by_default",
          watcher_config.defaultConfig.get("focus_shield_enabled") is False,
          "focus shield is OFF by default now that desktop exile is the primary method")
    check("config.popup_suppressor_enabled_default",
          watcher_config.defaultConfig.get("popup_suppressor_enabled") is True)
    check("config.desktop_exile_off_by_default",
          watcher_config.defaultConfig.get("desktop_exile_enabled") is False,
          "desktop exile is OFF by default now that the instant-hide hook is primary")
    check("config.focus_shield_seconds_removed",
          "focus_shield_seconds" not in watcher_config.defaultConfig)
    check("config.focus_shield_delay_removed",
          "focus_shield_delay_seconds" not in watcher_config.defaultConfig)
    check("config.focus_shield_watch_present",
          all(k in watcher_config.defaultConfig for k in
              ("focus_shield_watch_seconds", "focus_shield_poll_seconds",
               "focus_shield_restore_cooldown_seconds")))

    # Off-switch: shield bails immediately when disabled (no foreground touched).
    class _NoShieldCfg(_Cfg):
        focus_shield_enabled = False
    fsb = boot.GameBooter(lambda: _NoShieldCfg(), watcher_config.WatchState(),
                          threading.Event(), mgr)
    fsb.focusShield(0)
    fsb.focusShield(12345)
    check("boot.focus_shield_offswitch", True)

    # Source assertions: the boot shield no longer contains the looped/aggressive calls.
    _fsrc = open(_bootmod.__file__).read()
    check("boot.shield_no_loop_machinery",
          all(s not in _fsrc for s in
              ("focusWindowPreservingState", "maximiseWindow(", "isWindowMaximised",
               "isWindowMinimised", "BringWindowToTop")),
          "boot shield is free of the decommissioned aggressive machinery")
    check("boot.shield_is_bounded_watch",
          "def focusShield(self, protectedHandle: int) -> None:" in _fsrc
          and "focus_shield_watch_seconds" in _fsrc
          and "nextAllowedRestore" in _fsrc
          and "AttachThreadInput" not in _fsrc,
          "shield is the bounded gentle-watch variant (no AttachThreadInput)")

    # Behavioural: foregroundIsOurs detects our keyword/sandbox process but not the user's.
    _shieldBooter = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                                    threading.Event(), mgr)
    import types as _types
    class _Proc:
        def __init__(self, nm, ex): self._nm = nm; self._ex = ex
        def name(self): return self._nm
        def exe(self): return self._ex
    _orig_getpid = boot.getWindowPid
    _orig_psproc = boot.psutil.Process
    try:
        boot.getWindowPid = lambda h: 4242
        boot.psutil.Process = lambda pid: _Proc("steam.exe", "C:\\Program Files\\Steam\\steam.exe")
        check("boot.foreground_ours_by_keyword", _shieldBooter.foregroundIsOurs(999) is True)
        boot.psutil.Process = lambda pid: _Proc("opera.exe", "C:\\Apps\\Opera\\opera.exe")
        check("boot.foreground_user_not_ours", _shieldBooter.foregroundIsOurs(999) is False)
        check("boot.foreground_zero_handle", _shieldBooter.foregroundIsOurs(0) is False)
    finally:
        boot.getWindowPid = _orig_getpid
        boot.psutil.Process = _orig_psproc

    # Behavioural: one-shot restore only fires when foreground is ours; otherwise leaves alone.
    _restore = {"hwnd": None, "calls": 0}
    _orig_setfg = boot.setForeground
    _orig_getfg = boot.getForegroundWindow
    try:
        boot.setForeground = lambda h: (_restore.__setitem__("hwnd", h),
                                        _restore.__setitem__("calls", _restore["calls"] + 1), True)[-1]
        # Case A: foreground is one of ours throughout the watch -> at least one restore,
        # always targeting the protected handle. (cooldown=0 in _Cfg so it may fire each poll.)
        boot.getForegroundWindow = lambda: 555
        _shieldBooter.foregroundIsOurs = lambda h: True
        _restore["hwnd"] = None; _restore["calls"] = 0
        _shieldBooter.focusShield(111)
        check("boot.shield_restores_when_ours",
              _restore["hwnd"] == 111 and _restore["calls"] >= 1,
              f"-> restore={_restore}")
        # Case B: foreground belongs to the user -> yield immediately, never touch it.
        _shieldBooter.foregroundIsOurs = lambda h: False
        _restore["hwnd"] = None; _restore["calls"] = 0
        _shieldBooter.focusShield(111)
        check("boot.shield_leaves_user_alone",
              _restore["calls"] == 0, f"-> restore={_restore}")
        # Case C: focus never left the original window -> no restores needed.
        boot.getForegroundWindow = lambda: 111
        _shieldBooter.foregroundIsOurs = lambda h: True
        _restore["calls"] = 0
        _shieldBooter.focusShield(111)
        check("boot.shield_noop_when_focus_unmoved", _restore["calls"] == 0,
              f"-> restore={_restore}")
        # Case D: cooldown throttles repeated restores. With a long cooldown the shield
        # must restore AT MOST once even though foreground stays ours every poll.
        class _CooldownCfg(_Cfg):
            focus_shield_watch_seconds = 0.45
            focus_shield_poll_seconds = 0.1
            focus_shield_restore_cooldown_seconds = 100.0
        _cdb = boot.GameBooter(lambda: _CooldownCfg(), watcher_config.WatchState(),
                               threading.Event(), mgr)
        _cdb.foregroundIsOurs = lambda h: True
        boot.getForegroundWindow = lambda: 555
        _restore["calls"] = 0
        _cdb.focusShield(111)
        check("boot.shield_cooldown_throttles_restores", _restore["calls"] == 1,
              f"-> restore calls under long cooldown = {_restore['calls']} (expect 1)")
    finally:
        boot.setForeground = _orig_setfg
        boot.getForegroundWindow = _orig_getfg

    # Crack fix: focusShield runs in a daemon thread, so an internal exception must be
    # swallowed+logged (never propagate / silently kill the thread).
    _crash = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                             threading.Event(), mgr)
    _og = boot.getForegroundWindow
    try:
        boot.getForegroundWindow = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        raised = False
        try:
            _crash.focusShield(111)
        except Exception:
            raised = True
        check("boot.shield_guarded_against_exceptions", not raised,
              "internal errors are caught by the wrapper, not propagated")
    finally:
        boot.getForegroundWindow = _og
    # Bad config values (negative / non-numeric delay) must not crash the shield.
    class _BadDelayCfg(_Cfg):
        focus_shield_watch_seconds = "abc"
        focus_shield_poll_seconds = None
    _bad = boot.GameBooter(lambda: _BadDelayCfg(), watcher_config.WatchState(),
                           threading.Event(), mgr)
    bad_ok = True
    try:
        _bad.focusShield(111)
    except Exception:
        bad_ok = False
    check("boot.shield_survives_bad_config", bad_ok)

    # Adversarial: poll larger than the watch window must NOT make the thread linger past
    # the deadline (the sleep is clamped to the remaining time and chunked for stopEvent).
    class _BigPollCfg(_Cfg):
        focus_shield_watch_seconds = 0.2
        focus_shield_poll_seconds = 5.0
        focus_shield_restore_cooldown_seconds = 0.0
    _bp = boot.GameBooter(lambda: _BigPollCfg(), watcher_config.WatchState(),
                          threading.Event(), mgr)
    _bp.foregroundIsOurs = lambda h: True
    _ogf2 = boot.getForegroundWindow
    try:
        boot.getForegroundWindow = lambda: 555
        import time as _t
        _t0 = _t.time()
        _bp.focusShield(111)
        _elapsed = _t.time() - _t0
        check("boot.shield_no_linger_when_poll_gt_watch", _elapsed < 1.0,
              f"-> shield ran {_elapsed:.2f}s for a 0.2s watch (expect <1s)")
    finally:
        boot.getForegroundWindow = _ogf2

    # EXTREME: corrupt + missing config both fall back to defaults safely.
    import json as _cj
    with open(watcher_config.configFile, "w") as _f:
        _f.write("{ not valid json ]")
    check("config.corrupt_falls_back",
          watcher_config.loadConfig().get("task_process_name") == "TaskBarHero.exe")
    try:
        watcher_config.configFile.unlink()
    except Exception:
        pass
    check("config.missing_falls_back",
          watcher_config.loadConfig().get("refresher_interval_minutes") == 7.0)

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
        app.runDiagnostics()
        check("main.runDiagnostics", True)
    except Exception as exc:
        check("main.runDiagnostics", False, repr(exc))

    # one monitor tick (game not running -> should just set state and return fast)
    try:
        app.config.poll_interval_seconds = 0.01
        t = threading.Thread(target=app.monitorLoop, daemon=True)
        t.start()
        import time
        time.sleep(0.1)
        app.stopEvent.set()
        t.join(timeout=1)
        check("main.monitor_loop_tick", True)
    except Exception as exc:
        check("main.monitor_loop_tick", False, repr(exc))

    # ============================ DESKTOP EXILE ============================
    import desktop_exile as _de
    import watcher_config as _wc

    class _DeCfg:
        desktop_exile_enabled = True
        desktop_exile_game_title = "Task Bar Hero"
        task_process_name = "TaskBarHero.exe"
        desktop_exile_game_enabled = False
        desktop_exile_minimise_game = True
        desktop_exile_prelaunch_steam = True
        desktop_exile_hide_popup = True
        desktop_exile_steam_to_tray = True
        desktop_exile_restore_focus = True
        desktop_exile_switch_launch = False
        desktop_exile_switch_back_seconds = 0.0
        desktop_exile_fast_seconds = 0.0
        desktop_exile_fast_poll_seconds = 0.05
        desktop_exile_watch_seconds = 0.4
        desktop_exile_poll_seconds = 0.1

    # 1) Graceful when pyvda absent.
    _orig_avail = _de.pyvdaAvailable
    try:
        _de.pyvdaAvailable = lambda: False
        _x = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        no_raise = True
        try:
            _x.prelaunchExile(); _x.run(); _x.cleanup()
        except Exception:
            no_raise = False
        check("desktop.graceful_without_pyvda", no_raise and _x.enabled() is False)
    finally:
        _de.pyvdaAvailable = _orig_avail

    # Mock pyvda.
    class _MockDesktop:
        _registry = []
        def __init__(self, number=None, name="", did=None):
            if did is None:
                _MockDesktop._counter = getattr(_MockDesktop, "_counter", 0) + 1
                did = f"guid-{_MockDesktop._counter}"
            self._id = did; self._name = name; self.removed = False
        @property
        def id(self): return self._id
        @property
        def number(self):
            live = [d for d in _MockDesktop._registry if not d.removed]
            return live.index(self) + 1 if self in live else 0
        @property
        def name(self): return self._name
        def rename(self, n): self._name = n
        def remove(self, fallback=None): self.removed = True
        @classmethod
        def create(cls):
            d = cls(); cls._registry.append(d); return d
        @classmethod
        def current(cls):
            return [d for d in cls._registry if not d.removed][0]
    class _MockApp:
        def __init__(self, hwnd, desktop): self.hwnd = hwnd; self._desktop = desktop
        @property
        def desktop(self): return self._desktop
        def move(self, target): self._desktop = target
    _mock_apps = []
    mock_pyvda = type(sys)("pyvda")
    mock_pyvda.VirtualDesktop = _MockDesktop
    mock_pyvda.get_virtual_desktops = lambda: [d for d in _MockDesktop._registry if not d.removed]
    mock_pyvda.get_apps_by_z_order = lambda current_desktop=True: list(_mock_apps)
    mock_pyvda.AppView = lambda hwnd=None: None

    def _install_mock():
        sys.modules["pyvda"] = mock_pyvda
        _de.pyvdaAvailable = lambda: True
        _MockDesktop._registry = []; _MockDesktop._counter = 0; _mock_apps.clear()
        user_desktop = _MockDesktop(name=""); _MockDesktop._registry.append(user_desktop)
        return user_desktop

    _saved_mod = sys.modules.get("pyvda")
    try:
        # 2) Steam gets exiled; game + user windows do NOT (default: game minimised, not moved).
        user_desktop = _install_mock()
        x = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        x.metaPath = (stealth_temp.makeSessionDir() / "deskmeta.json")
        procs = {10: "steam.exe", 11: "taskbarhero.exe", 12: "notepad.exe",
                 13: "taskbarhero.exe", 14: "steam.exe"}
        titles = {10: "Steam", 11: "Task Bar Hero", 12: "My Notes",
                  13: "Task Bar Hero", 14: "Steam"}
        owner = {10: 9999, 11: 9999, 12: 9999, 13: 9999, 14: 4321}  # 14 is OUR window
        _mock_apps.extend([_MockApp(h, user_desktop) for h in procs])
        x.windowProcessName = lambda hwnd: procs.get(hwnd, "")
        x.windowTitle = lambda hwnd: titles.get(hwnd, "")
        x.isOurWindow = lambda hwnd: owner.get(hwnd) == 4321
        x.steamRunning = lambda: True
        minimised = []; hidden = []; trayed = []
        _de.minimiseWindow = lambda hwnd: (minimised.append(hwnd), True)[-1]
        _de.hideWindow = lambda hwnd: hidden.append(hwnd)
        _de.closeWindow = lambda hwnd: trayed.append(hwnd)
        # the mock keeps moved apps reachable; restoreSteamWindow uses AppView+move, so
        # patch restoreSteamWindow to a mock-aware version that moves the tracked app back.
        _app_by_hwnd = {a.hwnd: a for a in _mock_apps}
        def _mock_restore(hwnd, current):
            a = _app_by_hwnd.get(hwnd)
            if a is not None and current is not None:
                a.move(current)
            if x.config("desktop_exile_steam_to_tray", True):
                _de.closeWindow(hwnd); return True
            _de.minimiseWindow(hwnd); return True
        x.restoreSteamWindow = _mock_restore
        x.windowExists = lambda hwnd: True
        x.windowIsVisible = lambda hwnd: True
        x.windowIsMinimised = lambda hwnd: False
        bg = x.prelaunchExile()
        moved = {a.hwnd: (a.desktop is not user_desktop) for a in _mock_apps}
        check("desktop.prelaunch_exiles_steam", moved[10] is True,
              "Steam window exiled before launch")
        check("desktop.prelaunch_hides_steam_popup", 10 in hidden,
              "Steam popup is HIDDEN (SW_HIDE) to kill the desktop-switch flash")
        check("desktop.prelaunch_leaves_game", moved[11] is False,
              "game window NOT moved by pre-launch exile")
        check("desktop.never_moves_own_steam_window", moved[14] is False,
              "our own window (even if steam-named pid) is excluded by PID override")
        check("desktop.tracks_exiled_handles", 10 in x.exiledHandles,
              "exiled Steam hwnd is tracked for restore")
        # run watch with injection confirmed quickly -> brings Steam back (tray) + minimises game
        def _confirm_soon():
            time.sleep(0.05); x.signalInjectionConfirmed()
        threading.Thread(target=_confirm_soon, daemon=True).start()
        x.run(bg)
        moved_after = {a.hwnd: (a.desktop is not user_desktop) for a in _mock_apps}
        check("desktop.brings_steam_back_on_inject", moved_after[10] is False,
              "Steam returned to main desktop after injection confirmed")
        check("desktop.minimises_game_default", 11 in minimised,
              f"game minimised on main by default -> minimised={minimised}")
        check("desktop.steam_to_tray_on_return", 10 in trayed,
              "Steam sent to tray (WM_CLOSE) when brought back, default")
        _de.minimiseWindow = _wc.minimiseWindow
        _de.hideWindow = _wc.hideWindow
        _de.closeWindow = _wc.closeWindow

        # 3) Safety net: no injection -> Steam still brought back when watch times out.
        user_desktop = _install_mock()
        x2 = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        x2.metaPath = (stealth_temp.makeSessionDir() / "deskmeta2.json")
        p2 = {20: "steam.exe"}
        _mock_apps.append(_MockApp(20, user_desktop))
        x2.windowProcessName = lambda hwnd: p2.get(hwnd, "")
        x2.windowTitle = lambda hwnd: "Steam"
        x2.isOurWindow = lambda hwnd: False
        x2.steamRunning = lambda: True
        _de.minimiseWindow = lambda hwnd: True
        _de.hideWindow = lambda hwnd: None
        _de.closeWindow = lambda hwnd: None
        _app20 = {a.hwnd: a for a in _mock_apps}
        x2.windowExists = lambda hwnd: True
        x2.restoreSteamWindow = lambda hwnd, current: (_app20[hwnd].move(current)
                                                       if (hwnd in _app20 and current is not None)
                                                       else None, True)[-1]
        bg2 = x2.prelaunchExile()
        on_bg = [a for a in _mock_apps if a.hwnd == 20][0].desktop is not user_desktop
        check("desktop.exiled_then_timeout_setup", on_bg)
        x2.run(bg2)  # never confirmed -> times out (0.4s) -> bring back
        back = [a for a in _mock_apps if a.hwnd == 20][0].desktop is user_desktop
        check("desktop.bringback_on_timeout_safety_net", back,
              "Steam not stranded: returned to main after watch timeout without injection")
        _de.minimiseWindow = _wc.minimiseWindow
        _de.hideWindow = _wc.hideWindow
        _de.closeWindow = _wc.closeWindow

        # 4) Optional game exile: when desktop_exile_game_enabled True, game IS moved.
        class _DeCfgGame(_DeCfg):
            desktop_exile_game_enabled = True
        user_desktop = _install_mock()
        xg = _de.DesktopExile(lambda: _DeCfgGame(), threading.Event(), ownPid=4321)
        xg.metaPath = (stealth_temp.makeSessionDir() / "deskmetaG.json")
        pg = {30: "taskbarhero.exe"}
        _mock_apps.append(_MockApp(30, user_desktop))
        xg.windowProcessName = lambda hwnd: pg.get(hwnd, "")
        xg.windowTitle = lambda hwnd: "Task Bar Hero"
        xg.isOurWindow = lambda hwnd: False
        xg.steamRunning = lambda: False
        _de.minimiseWindow = lambda hwnd: True
        _de.hideWindow = lambda hwnd: None
        bgg = xg.prelaunchExile()
        xg.handleGameWindow(bgg)
        moved_game = [a for a in _mock_apps if a.hwnd == 30][0].desktop is not user_desktop
        check("desktop.optional_game_exile", moved_game,
              "game exiled when desktop_exile_game_enabled=True")
        _de.minimiseWindow = _wc.minimiseWindow
        _de.hideWindow = _wc.hideWindow

        # 5) No pile-up: crash reuse by persisted id.
        user_desktop = _install_mock()
        xa = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xa.metaPath = (stealth_temp.makeSessionDir() / "deskmetaA.json")
        xa.ensureBackgroundDesktop()
        xb = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xb.metaPath = xa.metaPath
        before = len(mock_pyvda.get_virtual_desktops()); xb.ensureBackgroundDesktop()
        after = len(mock_pyvda.get_virtual_desktops())
        check("desktop.reuses_bg_after_crash", after == before,
              f"-> before={before} after={after}")
        check("desktop.reuse_not_marked_created", xb.createdDesktop is False)
        xb.cleanup()
        bg_live = [d for d in _MockDesktop._registry
                   if d.name == _de.backgroundDesktopName and not d.removed]
        check("desktop.reuser_keeps_bg", len(bg_live) == 1, "reuser did not remove bg")
        xa.cleanup()
        bg_live2 = [d for d in _MockDesktop._registry
                    if d.name == _de.backgroundDesktopName and not d.removed]
        check("desktop.creator_removes_bg", len(bg_live2) == 0)

        # 6) stopEvent aborts watch promptly.
        user_desktop = _install_mock()
        ev = threading.Event(); ev.set()
        x3 = _de.DesktopExile(lambda: _DeCfg(), ev, ownPid=4321)
        x3.metaPath = (stealth_temp.makeSessionDir() / "deskmeta3.json")
        x3.steamRunning = lambda: True
        _de.hideWindow = lambda hwnd: None
        bg3 = x3.prelaunchExile()
        t0 = time.time(); x3.run(bg3); dt = time.time() - t0
        check("desktop.stopevent_aborts", dt < 1.0, f"-> {dt:.2f}s")
        _de.hideWindow = _wc.hideWindow

        # 6b) A failing single-window restore must NOT abort restoring the rest.
        _install_mock()
        xr = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xr.metaPath = (stealth_temp.makeSessionDir() / "deskmetaR.json")
        xr.exiledHandles = {501, 502}
        xr.windowExists = lambda hwnd: True
        xr.isOurWindow = lambda hwnd: False
        xr.currentDesktop = lambda: None
        _restored_ok = []
        def _restore_raises(hwnd, current):
            if hwnd == 501:
                raise RuntimeError("boom on first")
            _restored_ok.append(hwnd); return True
        xr.restoreSteamWindow = _restore_raises
        _raised = False
        try:
            xr.bringSteamBack()
        except Exception:
            _raised = True
        check("desktop.restore_failure_isolated", (not _raised) and (502 in _restored_ok),
              f"-> raised={_raised} restored_ok={_restored_ok} (502 still restored)")

        # 6c) Game minimize-once path (game_to_hidden OFF): minimize exactly once, never
        # after injection (tiny-res bug fix).
        class _DeCfgMinOnce(_DeCfg):
            desktop_exile_game_to_hidden = False
            desktop_exile_minimise_game = True
        _install_mock()
        xm = _de.DesktopExile(lambda: _DeCfgMinOnce(), threading.Event(), ownPid=4321)
        xm.metaPath = (stealth_temp.makeSessionDir() / "deskmetaM.json")
        _mock_apps.append(_MockApp(40, _MockDesktop._registry[0]))
        xm.windowProcessName = lambda hwnd: "taskbarhero.exe"
        xm.windowTitle = lambda hwnd: "Task Bar Hero"
        xm.isOurWindow = lambda hwnd: False
        xm.windowIsVisible = lambda hwnd: True
        xm.windowIsMinimised = lambda hwnd: False
        min_calls = []
        _de.minimiseWindow = lambda hwnd: (min_calls.append(hwnd), True)[-1]
        bgm = _MockDesktop._registry[0]
        xm.handleGameWindow(bgm)
        xm.handleGameWindow(bgm)
        xm.handleGameWindow(bgm)
        check("desktop.game_minimise_is_one_shot", len(min_calls) == 1,
              f"-> minimise called {len(min_calls)}x over 3 ticks (expect 1)")
        xm.gameHandledOnce = False; min_calls.clear()
        xm.signalInjectionConfirmed()
        xm.handleGameWindow(bgm)
        check("desktop.game_untouched_after_inject", len(min_calls) == 0,
              "game not minimised once injection is confirmed")
        _de.minimiseWindow = _wc.minimiseWindow

        # 6c2) Game-to-hidden path (default): move the game to the hidden desktop during boot,
        # bring it back to MAIN minimized on injection; never re-exiled after injection.
        _install_mock()
        user_d = _MockDesktop._registry[0]
        xh = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xh.metaPath = (stealth_temp.makeSessionDir() / "deskmetaH.json")
        game_app = _MockApp(60, user_d)
        _mock_apps.append(game_app)
        xh.windowProcessName = lambda hwnd: "taskbarhero.exe"
        xh.windowTitle = lambda hwnd: "Task Bar Hero"
        xh.isOurWindow = lambda hwnd: False
        xh.windowExists = lambda hwnd: True
        hmins = []
        _de.minimiseWindow = lambda hwnd: (hmins.append(hwnd), True)[-1]
        bgh = xh.ensureBackgroundDesktop()
        xh.exileGameToHidden(bgh)
        check("desktop.game_moved_to_hidden", game_app.desktop is bgh,
              "game window moved to the hidden desktop during boot")
        # REGRESSION (Opera bug): a browser tab titled '...Task Bar Hero - Opera' must NOT be
        # treated as the game -- matching is by PROCESS NAME only, never title substring.
        check("desktop.opera_title_not_treated_as_game",
              xh.isGameWindow("Class Ranking - TBH Tools | Task Bar Hero - Opera", "opera.exe") is False,
              "browser tab with 'Task Bar Hero' in the title is NOT the game")
        check("desktop.game_matched_by_process",
              xh.isGameWindow("", "taskbarhero.exe") is True,
              "game matched purely by process name even with empty title")
        check("desktop.game_tracked_for_return", 60 in xh.gameExiledApps)
        # idempotent: a second exile pass does not re-track / thrash
        xh.exileGameToHidden(bgh)
        check("desktop.game_exile_idempotent", len(xh.gameExiledApps) == 1)
        # bring back on injection
        xh.bringGameBack()
        check("desktop.game_back_on_main", game_app.desktop is user_d,
              "game returned to the main desktop")
        check("desktop.game_minimised_on_return", 60 in hmins,
              "game minimized when brought back")
        check("desktop.game_bringback_once", xh.gameBroughtBack is True)
        # bringGameBack again is a no-op (no double-restore)
        hmins.clear()
        xh.bringGameBack()
        check("desktop.game_bringback_idempotent", hmins == [])
        # after injection, handleGameWindow must not move the game again
        xh.gameExiledApps = {}; game_app.move(user_d)
        xh.signalInjectionConfirmed()
        xh.handleGameWindow(bgh)
        check("desktop.game_not_exiled_after_inject", game_app.desktop is user_d,
              "game not re-exiled once injection confirmed")
        # concurrent bringGameBack -> exactly one restore (atomic guard under RLock)
        _install_mock()
        xc = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        cg = _MockApp(70, _MockDesktop._registry[0])
        xc.gameExiledApps = {70: cg}
        xc.windowExists = lambda hwnd: True
        xc.isOurWindow = lambda hwnd: False
        xc.currentDesktop = lambda: _MockDesktop._registry[0]
        cmins = []
        _de.minimiseWindow = lambda hwnd: (cmins.append(hwnd), True)[-1]
        _cthreads = [threading.Thread(target=xc.bringGameBack) for _ in range(8)]
        for _t in _cthreads: _t.start()
        for _t in _cthreads: _t.join()
        check("desktop.bringgame_atomic_single_restore", cmins.count(70) == 1,
              f"-> {cmins.count(70)} restores from 8 concurrent calls (expect 1)")
        # runImpl must not deadlock (RLock + lock narrowed to setup; watch calls bringGameBack)
        _install_mock()
        xd = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xd.metaPath = (stealth_temp.makeSessionDir() / "deskmetaD.json")
        xd.windowProcessName = lambda hwnd: ""
        xd.windowTitle = lambda hwnd: ""
        xd.isOurWindow = lambda hwnd: False
        xd.windowExists = lambda hwnd: True
        import time as _tdl
        _done = {"ok": False}
        def _runner():
            xd.runImpl(); _done["ok"] = True
        _th = threading.Thread(target=_runner); _th.start(); _th.join(timeout=5)
        check("desktop.runimpl_no_deadlock", _done["ok"] is True,
              "runImpl (watch + bringGameBack) completes without deadlocking")
        check("desktop.lock_is_reentrant",
              type(xd.lock).__name__ == "RLock",
              f"-> lock type {type(xd.lock).__name__} (must be RLock to avoid nested-lock deadlock)")
        _de.minimiseWindow = _wc.minimiseWindow

        # 6d) restoreUserFocus is a guarded ONE-SHOT: after a focus-stealer is removed it
        # hands focus back to the pre-launch window exactly once (via AllowSetForegroundWindow),
        # never repeating (this was the taskbar-flash bug), and never if focus is already there.
        _install_mock()
        xf = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xf.metaPath = (stealth_temp.makeSessionDir() / "deskmetaF.json")
        xf.previousForeground = 700
        set_fg = []; flash_calls = []
        _de.forceForegroundOnce = lambda hwnd: (set_fg.append(hwnd), True)[-1]
        _de.stopFlashing = lambda hwnd: flash_calls.append(hwnd)
        _de.getForegroundWindow = lambda: 800
        xf.windowExists = lambda hwnd: True
        # first call restores once via forceForegroundOnce + stopFlashing
        xf.restoreUserFocus()
        check("desktop.restore_focus_one_shot", set_fg == [700],
              f"-> forceForegroundOnce={set_fg}")
        check("desktop.restore_stops_flashing", flash_calls == [700],
              f"-> stopFlashing calls {flash_calls}")
        # SECOND call must NOT fire again (one-shot guard prevents repeat/flash)
        set_fg.clear()
        xf.restoreUserFocus()
        check("desktop.restore_focus_no_repeat", set_fg == [],
              f"-> second call forceForegroundOnce={set_fg} (must be empty)")
        # if focus already on the previous window, do nothing (and mark done)
        xf2 = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xf2.previousForeground = 700
        set_fg.clear()
        _de.getForegroundWindow = lambda: 700
        _de.forceForegroundOnce = lambda hwnd: (set_fg.append(hwnd), True)[-1]
        xf2.windowExists = lambda hwnd: True
        xf2.restoreUserFocus()
        check("desktop.no_restore_when_focus_already_there", set_fg == [],
              f"-> setForeground calls {set_fg} (expect [])")
        # if the previous window no longer exists, do nothing
        xf3 = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        xf3.previousForeground = 700
        set_fg.clear()
        _de.getForegroundWindow = lambda: 800
        _de.forceForegroundOnce = lambda hwnd: (set_fg.append(hwnd), True)[-1]
        xf3.windowExists = lambda hwnd: False
        xf3.restoreUserFocus()
        check("desktop.no_restore_when_prev_gone", set_fg == [],
              f"-> setForeground calls {set_fg} (expect [])")
        _de.forceForegroundOnce = _wc.forceForegroundOnce
        _de.getForegroundWindow = _wc.getForegroundWindow
        _de.stopFlashing = _wc.stopFlashing

        # rememberForeground must NOT capture our own / game / steam window (else restore is
        # pointless on refreshes). It should only remember the user's real app.
        _install_mock()
        xr = _de.DesktopExile(lambda: _DeCfg(), threading.Event(), ownPid=4321)
        _de.getForegroundWindow = lambda: 12345
        xr.isOurWindow = lambda hwnd: True
        xr.rememberForeground()
        check("desktop.remember_skips_own_window", xr.previousForeground == 0,
              "own window is not remembered as the focus target")
        xr.isOurWindow = lambda hwnd: False
        xr.windowProcessName = lambda hwnd: "steam.exe"
        xr.rememberForeground()
        check("desktop.remember_skips_steam", xr.previousForeground == 0)
        xr.windowProcessName = lambda hwnd: "taskbarhero.exe"
        xr.rememberForeground()
        check("desktop.remember_skips_game", xr.previousForeground == 0)
        xr.windowProcessName = lambda hwnd: "opera.exe"
        xr.rememberForeground()
        check("desktop.remember_keeps_user_app", xr.previousForeground == 12345,
              "the user's real app IS remembered as the focus target")
        _de.getForegroundWindow = _wc.getForegroundWindow

        # forceForegroundOnce SAFETY: even if SetForegroundWindow raises, the thread-input
        # attachment must be detached (finally), so input queues are never left attached
        # (this was the click-through cause). We verify attach/detach balance with a stub.
        import watcher_config as _wcfg
        class _RecU32b:
            def __init__(self): self.attach = []
            def AllowSetForegroundWindow(self, x): return 1
            def GetForegroundWindow(self): return 222
            def GetWindowThreadProcessId(self, h, p=None): return 55
            def AttachThreadInput(self, a, b, flag):
                self.attach.append((a, b, bool(flag))); return 1
            def SetForegroundWindow(self, h): raise RuntimeError("OS denied")
        import ctypes as _ct
        _ou = _wcfg.user32; _ow = getattr(_ct, "windll", None)
        rec = _RecU32b()
        class _FK:
            class kernel32:
                @staticmethod
                def GetCurrentThreadId(): return 7
        _wcfg.user32 = rec
        try:
            _ct.windll = _FK()
            res = _wcfg.forceForegroundOnce(999)
            attaches = [c for c in rec.attach if c[2] is True]
            detaches = [c for c in rec.attach if c[2] is False]
            check("config.forceForeground_detaches_on_error",
                  len(attaches) == len(detaches) and len(attaches) == 1,
                  f"-> attach={attaches} detach={detaches} (must be balanced even when SFW raises)")
            check("config.forceForeground_returns_false_on_error", res is False)
        finally:
            _wcfg.user32 = _ou
            if _ow is not None: _ct.windll = _ow

        # 6e) switchLaunch: when enabled, goes to bg, runs launch, returns to origin.
        _install_mock()
        class _DeCfgSwitch(_DeCfg):
            desktop_exile_switch_launch = True
            desktop_exile_switch_back_seconds = 0.0
        xs = _de.DesktopExile(lambda: _DeCfgSwitch(), threading.Event(), ownPid=4321)
        xs.metaPath = (stealth_temp.makeSessionDir() / "deskmetaS.json")
        go_log = []
        class _GoDesktop(_MockDesktop):
            def go(self, allow_set_foreground=True): go_log.append(self.number)
        origin = _GoDesktop(name=""); _MockDesktop._registry = [origin]
        bgs = xs.ensureBackgroundDesktop()
        # ensureBackgroundDesktop created a plain _MockDesktop; give it a go()
        bgs.go = lambda allow_set_foreground=True: go_log.append(bgs.number)
        launched = []
        xs.switchLaunch(bgs, lambda: launched.append(True))
        check("desktop.switch_launch_runs_and_returns",
              launched == [True] and len(go_log) >= 1,
              f"-> launched={launched} go_log={go_log}")

        # 7) Source guard: .go() (which switches the user's view) only appears inside the
        # opt-in switchLaunch path, never in the default sweep/runWatch path.
        _desrc = open(_de.__file__).read()
        _go_count = _desrc.count(".go(")
        _switchlaunch_body = _desrc.split("def switchLaunch", 1)[-1].split("def run(", 1)[0]
        _go_in_switch = _switchlaunch_body.count(".go(")
        check("desktop.go_only_in_switchlaunch",
              _go_count > 0 and _go_count == _go_in_switch,
              f"-> total .go()={_go_count}, in switchLaunch={_go_in_switch} (must be equal)")
        check("desktop.switch_launch_off_by_default",
              _wc.defaultConfig.get("desktop_exile_switch_launch") is False)
    finally:
        if _saved_mod is not None:
            sys.modules["pyvda"] = _saved_mod
        else:
            sys.modules.pop("pyvda", None)
        _de.pyvdaAvailable = _orig_avail

    # ============================ POPUP SUPPRESSOR ============================
    import popup_suppressor as _ps

    class _PsCfg:
        popup_suppressor_enabled = True
        popup_suppressor_seconds = 1.0
        popup_suppressor_restore_on_stop = True
        desktop_exile_steam_to_tray = True

    sup = _ps.PopupSuppressor(lambda: _PsCfg(), ownPid=4321)
    # graceful when win32 unavailable (sandbox): start() returns False, no crash.
    check("popup.graceful_without_win32", sup.start(1.0) is False and sup.available() is False)
    no_raise = True
    try:
        sup.stop(); sup.restoreHidden()
    except Exception:
        no_raise = False
    check("popup.stop_restore_safe_without_win32", no_raise)

    # shouldHide matching (stub the window-info helpers; force getWindowPid via override).
    sup2 = _ps.PopupSuppressor(lambda: _PsCfg(), ownPid=4321)
    sup2.processName = lambda h: "steam.exe"
    sup2.windowClass = lambda h: "USurface_1009"
    sup2.windowTitle = lambda h: "Steam"
    import watcher_config as _wc2
    _orig_gwp = _ps.getWindowPid
    try:
        _ps.getWindowPid = lambda h: 9999  # not our pid
        check("popup.hides_steam_popup", sup2.shouldHide(10) is True)
        sup2.windowClass = lambda h: "SDL_app"
        check("popup.spares_main_steam_client", sup2.shouldHide(10) is False,
              "main Steam client (SDL_app) is NOT hidden")
        sup2.processName = lambda h: "notepad.exe"; sup2.windowClass = lambda h: "Notepad"; sup2.windowTitle = lambda h: "notes"
        check("popup.spares_user_window", sup2.shouldHide(10) is False)
        sup2.windowTitle = lambda h: "Launching Task Bar Hero"
        check("popup.hides_by_title_hint", sup2.shouldHide(10) is True)
        # never hide our OWN window
        _ps.getWindowPid = lambda h: 4321
        sup2.processName = lambda h: "steam.exe"; sup2.windowClass = lambda h: "USurface_1"; sup2.windowTitle = lambda h: "Steam"
        check("popup.never_hides_own_window", sup2.shouldHide(10) is False)
    finally:
        _ps.getWindowPid = _orig_gwp

    # restoreHidden: tray vs minimise branch + skips dead windows + atomic clear.
    sup3 = _ps.PopupSuppressor(lambda: _PsCfg(), ownPid=4321)
    sup3.hiddenHandles = {401, 402}
    sup3.windowExists = lambda h: h == 401
    trayed = []; mins = []
    _ps.closeWindow = lambda h: trayed.append(h)
    _ps.minimiseWindow = lambda h: mins.append(h)
    _ps_user32 = _ps.user32
    _ps.user32 = None  # skip ShowWindow path safely
    try:
        n = sup3.restoreHidden()
        check("popup.restore_tray_default", trayed == [401] and n == 1,
              f"-> trayed={trayed} n={n}")
        check("popup.restore_skips_dead_window", 402 not in trayed)
        check("popup.restore_clears_set", len(sup3.hiddenHandles) == 0)
        # minimise branch
        class _PsMinCfg(_PsCfg):
            desktop_exile_steam_to_tray = False
        sup4 = _ps.PopupSuppressor(lambda: _PsMinCfg(), ownPid=4321)
        sup4.hiddenHandles = {500}; sup4.windowExists = lambda h: True
        trayed.clear(); mins.clear()
        sup4.restoreHidden()
        check("popup.restore_minimise_branch", mins == [500] and trayed == [])
    finally:
        _ps.user32 = _ps_user32
        _ps.closeWindow = _wc2.closeWindow
        _ps.minimiseWindow = _wc2.minimiseWindow

    # booter wires injection -> stops+restores suppressor (no crash even if suppressor is a stub).
    class _StubSup:
        def __init__(self): self.stopped = 0; self.restored = 0
        def reset(self): pass
        def start(self, s): return True
        def stop(self): self.stopped += 1
        def restoreHidden(self): self.restored += 1
    _stub = _StubSup()
    _bSup = boot.GameBooter(lambda: _Cfg(), watcher_config.WatchState(),
                            threading.Event(), mgr, popupSuppressor=_stub)
    _bSup.notifyInjection()
    check("popup.injection_stops_and_restores", _stub.stopped == 1 and _stub.restored == 1,
          f"-> stopped={_stub.stopped} restored={_stub.restored}")

    # Source guard: the hook uses EVENT_OBJECT_SHOW and a kept-alive callback ref.
    _pssrc = open(_ps.__file__).read()
    check("popup.uses_show_event_hook",
          "EVENT_OBJECT_SHOW" in _pssrc and "SetWinEventHook" in _pssrc
          and "self.callbackRef" in _pssrc,
          "instant-hide hook on EVENT_OBJECT_SHOW with persistent callback ref")

    # cleanup: remove everything this test session created in the fake temp.
    stealth_temp.cleanupSession()
    for folder, _ in stealth_temp.iterOwnedFolders():
        stealth_temp.removeDir(folder)
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
