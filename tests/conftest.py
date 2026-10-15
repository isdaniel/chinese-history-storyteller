import os
import subprocess
import types

import pytest

import shots_guard
from common import ffmpeg_bin

# Reasons of the skips that mean "this machine lacks a tool the tests need" (see ffmpeg_exe below, the font
# checks in test_render_*.py and the SIGALRM check in test_shots_guard.py). On CI such a skip must fail the
# session: those tests would otherwise silently not run and the job would stay green.
MISSING_TOOL_SKIP_REASONS = ("ffmpeg is not available", "no CJK font installed", "SIGALRM is not available")
GUARD_PLUGIN = pytest.StashKey()


@pytest.fixture(autouse=True)
def no_real_retry_sleep():
    """shots 階段重試的間隔是 30/60 秒:任何測試都不該真的睡 (test_shots_guard 會再換成會記錄的版本)。

    刻意不用 monkeypatch fixture:它會讓這個 autouse fixture 的 monkeypatch 最晚才還原,
    打亂其他測試模組自己的 fixture 還原順序 (例如 test_render_overlays 的字型快取)。"""
    original = shots_guard.time
    shots_guard.time = types.SimpleNamespace(sleep=lambda seconds: None)
    yield
    shots_guard.time = original


def _running_on_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false")


class MissingToolSkips:
    def __init__(self):
        self.reports = []

    def pytest_runtest_logreport(self, report):
        longrepr = report.longrepr
        text = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else ""
        if report.skipped and any(reason in text for reason in MISSING_TOOL_SKIP_REASONS):
            self.reports.append((report.nodeid, text.removeprefix("Skipped: ")))


def pytest_configure(config):
    if _running_on_ci():
        config.stash[GUARD_PLUGIN] = MissingToolSkips()
        config.pluginmanager.register(config.stash[GUARD_PLUGIN], "missing-tool-skips")


def pytest_sessionfinish(session, exitstatus):
    guard = session.config.stash.get(GUARD_PLUGIN, None)
    if guard is not None and guard.reports and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    guard = config.stash.get(GUARD_PLUGIN, None)
    if guard is None or not guard.reports:
        return
    terminalreporter.section("CI: tests skipped for missing tools", red=True, bold=True)
    terminalreporter.line("CI is set, so a missing ffmpeg or CJK font fails the session instead of silently skipping:")
    for nodeid, reason in guard.reports:
        terminalreporter.line(f"  {nodeid}: {reason}")
    terminalreporter.line("Install ffmpeg and fonts-noto-cjk (see .github/workflows/test.yml).")


@pytest.fixture(scope="session")
def ffmpeg_exe():
    exe = ffmpeg_bin()
    try:
        subprocess.run([exe, "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("ffmpeg is not available")
    return exe
