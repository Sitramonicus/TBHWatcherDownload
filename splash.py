from __future__ import annotations

from watcher_config import logInfo


class Splash:
    def __init__(self, totalSteps: int = 6, title: str = "TBH Watcher") -> None:
        self.totalSteps = max(1, totalSteps)
        self.title = title
        self.stepsDone = 0
        self.lastLabel = ""

    def start(self) -> None:
        logInfo("[splash] Startup beginning (progress will be logged; no window).")

    def step(self, label: str) -> None:
        self.stepsDone = min(self.stepsDone + 1, self.totalSteps)
        percent = int(self.stepsDone / self.totalSteps * 100)
        self.lastLabel = label
        logInfo(f"[splash] ({percent}%) {label}")

    def message(self, label: str) -> None:
        self.lastLabel = label
        logInfo(f"[splash] {label}")

    def finish(self) -> None:
        logInfo("[splash] Startup complete.")

    def notifyReady(self, icon=None) -> None:
        if icon is None:
            return
        try:
            notify = getattr(icon, "notify", None)
            if callable(notify):
                notify("TBH Watcher is running.", "TBH Watcher")
        except Exception as error:
            logInfo(f"[splash] tray notify failed (ignored): {error}")


def diagnose(report) -> list:
    issues = []
    report("splash.mode", "PASS", "log + tray balloon (no window created)")
    try:
        splash = Splash(totalSteps=3)
        splash.start()
        splash.step("probe")
        splash.message("probe")
        splash.notifyReady(None)
        splash.finish()
        report("splash.api", "PASS", "start/step/message/notify/finish callable")
    except Exception as error:
        report("splash.api", "FAIL", str(error))
        issues.append("splash api raised")
    return issues


if __name__ == "__main__":
    def printer(component, status, detail):
        print(f"[{status:4}] {component:24} {detail}")
    foundIssues = diagnose(printer)
    print("OK" if not foundIssues else f"ISSUES: {foundIssues}")
