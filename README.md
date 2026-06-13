# TBH Watcher

A lightweight Windows tray utility that keeps **Task Bar Hero** running the way you
want it to: it watches the game, recovers it if it hangs, refreshes it on a schedule,
and quietly boots the Speedy (OpenSpeedy) speed helper for you — without the game's
launch popping into your face or stealing what you're typing.

It runs in the system tray (no window), survives unattended sessions, and is built so
that any single feature failing never takes the whole tool down.
---

## Requirements

### OpenSpeedy
```winget install openspeedy```
https://github.com/game1024/OpenSpeedy/

### Sandboxie (Optional)
https://github.com/sandboxie-plus/sandboxie

---

## What it does

- **Hang recovery** — detects when the game stops responding and restarts it for you.
- **Scheduled refresh** — on an interval (with a little randomisation), it closes the
  game, waits a cooldown, and relaunches it, re‑applying the speed helper each time.
- **Silent Speedy boot** — launches OpenSpeedy in the background and confirms the
  speed patch actually took, re‑applying it once automatically if it didn't.
- **Stays out of your way during launch** — Steam's "launching…" popup is hidden the
  instant it appears, and the game boots onto a hidden virtual desktop and comes back
  minimised once it's ready, so your screen isn't interrupted.
- **Keeps your typing focus** — if the launch grabs the foreground, it hands focus back
  to the window you were using, so you can keep typing in your browser, etc.
- **Works while the PC is locked** — the important parts (refresh, speed‑patch, hang
  recovery) keep running when you step away; the on‑screen niceties are skipped while
  locked since nobody's watching the screen.
- **Self‑healing config** — your settings file is checked on every start: your valid
  choices are kept, settings that no longer exist are removed, brand‑new settings get
  sensible defaults, and any value that's been set to something invalid is reset to its
  default on its own (without touching your other settings). The previous config is
  backed up first.
- **Auto-revive on crash** — if the game closes unexpectedly (not during a scheduled refresh,
  and not a freeze), the watcher relaunches it automatically. Optionally it can first ask “Did
  you close it?” and only relaunch if you don’t answer within a few seconds.
- **Set the game speed** — choose a speed multiplier (e.g. x2, x3, x10) from the tray and the
  watcher applies it to OpenSpeedy automatically each launch, without you opening Speedy.
- **Tray controls** — toggle the refresher, manage the speed‑helper camouflage, set the speed,
  force a quick refresh, open the config/log, run diagnostics, and see the current version.

---

## Changelog

### 3.7.0 — Adjust to be able to run a second copy inside Sandboxie

- You can now run **two independent game + watcher pairs at once**: one normally on your
  PC, and a second one inside a Sandboxie box. Each watcher minds only its own game and
  Speedy — the boxed watcher won't touch your host game, and vice versa.
- The watcher **detects when it's running in Sandboxie** and labels itself **"TBH Watcher (SB)"**
  in the tray, with a matching marker in the Debug menu, so you can tell the two apart at a glance.
- Each instance starts independently — running a copy in the box no longer blocks the normal one
  (or the other way around).
- **Focus return is skipped inside the box** (Sandboxie keeps boxed apps from reaching across to
  your normal windows, so it can't reliably hand focus back anyway), but the game still boots onto
  the hidden desktop and comes back minimised as usual.
- Known limitation: inside Sandboxie the **tray icon may not appear** (Sandboxie can block the
  icon from registering with Windows). The watcher still runs and does its job, but to get the
  tray icon back you may need a recent Sandboxie-Plus and to allow tray-icon/window access for the
  box (e.g. `UseShellNotifyIconProxy=y` + `OpenWinClass=*`, or set the box to Application
  Compartment mode).

### 3.6.2 — Auto-revive when the game crashes
- If Task Bar Hero closes unexpectedly (a real crash/close — not a scheduled refresh and not a
  freeze), the watcher now relaunches it automatically. Previously an unexpected exit was simply
  noticed and left closed.
- Optional confirmation: if you turn auto-revive off, the watcher first asks “Did you close it?”
  and only relaunches if you don’t answer within a few seconds (configurable; 5 seconds default).
- Reliability: the background monitor/refresher loops are hardened so they can’t silently stop,
  and the revive step is guarded so an error is logged rather than ending monitoring.
- Note: Game becomes unstable around x68.

### 3.6.1 — Set the game speed from the tray
- New **Speedy Speed** control: pick a multiplier (e.g. x2, x3, x10) and the watcher applies it
  to OpenSpeedy automatically before each launch. Your real OpenSpeedy install is never edited —
  the speed is applied to the disposable copy the watcher runs, and only the single speed value
  is changed (the rest of Speedy's settings file is left exactly as it was).
- The **Set Speed** menu has a **Custom…** option: type any multiplier (e.g. 25x) in a small
  popup, no config editing needed. Values above 70x ask for confirmation first, since running
  that fast can change game data quickly enough to look like manipulation to anti-cheat.

### 3.6.0 — Locked‑session support & self‑healing config
- The tool now keeps refreshing, re‑applying the speed patch, and recovering hangs even
  while your PC is **locked**; the on‑screen launch tidying is automatically skipped
  while locked.
- Your **config is now validated and repaired on start**: kept‑if‑valid, obsolete
  settings removed, new settings added with defaults, and individual bad values reset to
  default — with a backup of your previous config kept just in case.
- The **current version is now shown** in the Debug menu.

### 3.5.x — Reliable launch experience
- Steam's launch popup is hidden the **instant** it appears (no flash), instead of being
  chased after the fact.
- The game boots on a **hidden desktop** and returns **minimised** once the speed patch
  is confirmed, so the game never flashes you long during boot.
- **Input focus is reliably returned** to whatever you were using, so you can keep
  typing through a launch. Though reliant, there is around a second of lost focus which
  cannot be resolved at the moment.
- A browser tab or other window that merely has "Task Bar Hero" in its title is no
  longer mistaken for the game.

### 3.x — Reworked launch‑tidying approaches (internal iterations)
- Tried: Focus shield, then virtual‑desktop relocation, then the
  current instant‑hide + focus‑return combination that proved most reliable.

### 3.0.0 — Clean rebuild

### 2.0.0 — Verified working baseline
- RE: Hang recovery, scheduled refresh
- silent Speedy injection working end‑to‑end,
  with the speed‑helper DLL camouflaged inside an isolated temp sandbox and post‑inject
  verification that re‑applies the patch once if it didn't take.

### 1.x — Rough make
- Hang recovery, scheduled refresh
