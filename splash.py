from __future__ import annotations

from watcher_config import log

PERCENT_SCALE = 100
DEFAULT_STARTUP_PHASES = 6


class Splash:
    def __init__(self, total_phases: int = DEFAULT_STARTUP_PHASES, title: str = "TBH Watcher") -> None:
        self.total_phases = max(1, total_phases)
        self.title = title
        self.phases_done = 0
        self.last_message = ""

    def start(self) -> None:
        log("[splash] Startup beginning (progress is logged; no window is created).")

    def step(self, phase_label: str) -> None:
        self.phases_done = min(self.phases_done + 1, self.total_phases)
        percent = int(self.phases_done / self.total_phases * PERCENT_SCALE)
        self.last_message = phase_label
        log(f"[splash] ({percent}%) {phase_label}")

    def message(self, text: str) -> None:
        self.last_message = text
        log(f"[splash] {text}")

    def finish(self) -> None:
        log("[splash] Startup complete.")

    def notify_ready(self, tray_icon=None) -> None:
        if tray_icon is None:
            return
        try:
            show_balloon = getattr(tray_icon, "notify", None)
            if callable(show_balloon):
                show_balloon("TBH Watcher is running.", "TBH Watcher")
        except Exception as error:
            log(f"[splash] Tray notification failed (ignored): {error}")


def diagnose(report) -> list:
    problems = []
    report("splash.mode", "PASS", "log + tray balloon (no window created)")
    try:
        probe = Splash(total_phases=3)
        probe.start()
        probe.step("probe")
        probe.message("probe")
        probe.notify_ready(None)
        probe.finish()
        report("splash.api", "PASS", "start/step/message/notify/finish callable")
    except Exception as error:
        report("splash.api", "FAIL", str(error))
        problems.append("splash api raised")
    return problems


if __name__ == "__main__":
    def _print_row(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    found = diagnose(_print_row)
    print("OK" if not found else f"ISSUES: {found}")
