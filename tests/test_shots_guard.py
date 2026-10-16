"""shots 階段的重試與降級 (scripts/shots_guard.py) 以及三個階段、make_video、notify、本機 runner 的接線測試。"""
import json
import logging
import os
import signal
import sys
import threading
import time as real_time
from pathlib import Path

import pytest

import common
import generate_images
import ground_focus
import make_video
import notify
import plan_shots
import run_episode_local as local
import shots_guard as sg


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("SHOTS_STRICT", "SHOTS_STAGE_ATTEMPTS", "VIDEO_MODE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(sg.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(sg, "notify_discord", sent.append)
    return sent


def failing(times, result="done", error=RuntimeError("boom")):
    state = {"calls": 0}

    def fn():
        state["calls"] += 1
        if state["calls"] <= times:
            raise error
        return result

    fn.state = state
    return fn


# --- configuration ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(None, False), ("", False), ("0", False), ("false", False), ("No", False),
                                            ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
                                            (" 1 ", True), ("maybe", False)])
def test_strict_mode_is_read_from_the_environment(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SHOTS_STRICT", value)
    assert sg.strict() is expected


@pytest.mark.parametrize("value,expected", [(None, 3), ("", 3), ("1", 1), ("2", 2), ("5", 5), ("6", 5), ("99", 5),
                                            ("0", 3), ("-2", 3), ("abc", 3), ("2.5", 3), (" 4 ", 4)])
def test_attempts_are_read_from_the_environment_and_bounded(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SHOTS_STAGE_ATTEMPTS", value)
    assert sg.attempts() == expected


# --- run_stage -------------------------------------------------------------------------------------------------------

def test_a_stage_that_works_runs_once_without_noise(tmp_path, sleeps, alerts):
    fn = failing(0)
    assert sg.run_stage("plan", 7, tmp_path, fn) is True
    assert fn.state["calls"] == 1 and sleeps == [] and alerts == []
    assert sg.read_marker(tmp_path) is None


def test_a_transient_failure_is_retried_with_a_growing_pause(tmp_path, sleeps, alerts):
    fn = failing(2)
    assert sg.run_stage("images", 7, tmp_path, fn) is True
    assert fn.state["calls"] == 3
    assert sleeps == [30, 60]
    assert alerts == [] and sg.read_marker(tmp_path) is None


def test_a_stage_that_keeps_failing_degrades_after_the_last_attempt(tmp_path, sleeps, alerts):
    fn = failing(99, error=ValueError("bad data"))
    assert sg.run_stage("plan", 7, tmp_path, fn) is False
    assert fn.state["calls"] == 3
    assert sleeps == [30, 60], "no pause after the last attempt"
    marker = sg.read_marker(tmp_path)
    assert marker["stage"] == "plan" and marker["attempts"] == 3 and marker["error"] == "ValueError: bad data"
    assert sg.is_degraded(tmp_path)
    assert len(alerts) == 1 and "Episode 7" in alerts[0] and "「規劃」" in alerts[0]
    assert "降級" in alerts[0] and "ValueError: bad data" in alerts[0] and "嘗試 3 次" in alerts[0]


def test_the_number_of_attempts_follows_the_environment(tmp_path, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STAGE_ATTEMPTS", "1")
    fn = failing(99)
    assert sg.run_stage("plan", 1, tmp_path, fn) is False
    assert fn.state["calls"] == 1 and sleeps == []
    monkeypatch.setenv("SHOTS_STAGE_ATTEMPTS", "2")
    fn = failing(99)
    assert sg.run_stage("plan", 1, tmp_path, fn) is False
    assert fn.state["calls"] == 2 and sleeps == [30]


def test_a_non_fatal_stage_only_alerts_and_leaves_no_marker(tmp_path, sleeps, alerts):
    fn = failing(99)
    assert sg.run_stage("ground", 7, tmp_path, fn, fatal=False) is False
    assert fn.state["calls"] == 3
    assert sg.read_marker(tmp_path) is None and not sg.is_degraded(tmp_path)
    assert len(alerts) == 1 and "「聚焦」" in alerts[0] and "降級" not in alerts[0]


def test_strict_mode_retries_and_then_raises_the_last_error(tmp_path, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    fn = failing(99, error=KeyError("missing"))
    with pytest.raises(KeyError):
        sg.run_stage("plan", 7, tmp_path, fn)
    assert fn.state["calls"] == 3 and sleeps == [30, 60]
    assert sg.read_marker(tmp_path) is None and alerts == []


def test_strict_mode_still_lets_a_retry_succeed(tmp_path, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    assert sg.run_stage("plan", 7, tmp_path, failing(1)) is True


def test_keyboard_interrupt_is_not_swallowed(tmp_path, sleeps, alerts):
    fn = failing(99, error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        sg.run_stage("plan", 7, tmp_path, fn)
    assert fn.state["calls"] == 1 and sg.read_marker(tmp_path) is None and alerts == []


def test_a_failing_alert_never_hides_the_degrade(tmp_path, monkeypatch, sleeps):
    def broken_alert(message):
        raise RuntimeError("discord is down")

    monkeypatch.setattr(sg, "notify_discord", broken_alert)
    assert sg.run_stage("plan", 7, tmp_path, failing(99)) is False
    assert sg.is_degraded(tmp_path)


def test_the_alert_reason_is_truncated(tmp_path, sleeps, alerts):
    sg.run_stage("plan", 7, tmp_path, failing(99, error=RuntimeError("x" * 3000)))
    assert len(alerts[0]) < 800 and alerts[0].count("x") <= 500
    assert len(sg.read_marker(tmp_path)["error"]) <= 500


def test_a_blank_error_message_still_names_the_exception_type(tmp_path, sleeps, alerts):
    sg.run_stage("plan", 7, tmp_path, failing(99, error=RuntimeError()))
    assert sg.read_marker(tmp_path)["error"] == "RuntimeError"


# --- marker ----------------------------------------------------------------------------------------------------------

def test_a_marker_that_cannot_be_parsed_still_counts_as_degraded(tmp_path):
    (tmp_path / sg.MARKER).write_text("{not json", encoding="utf-8")
    assert sg.is_degraded(tmp_path)
    assert sg.read_marker(tmp_path)["stage"] == "unknown"


@pytest.mark.parametrize("payload", ["[]", "null", '"x"', "5", '{"stage": 3}'])
def test_an_odd_marker_is_degraded_with_an_unknown_stage(tmp_path, payload):
    (tmp_path / sg.MARKER).write_text(payload, encoding="utf-8")
    assert sg.is_degraded(tmp_path) and sg.read_marker(tmp_path)["stage"] == "unknown"


def test_clearing_a_marker_is_idempotent(tmp_path):
    sg.clear_marker(tmp_path)
    (tmp_path / sg.MARKER).write_text("{}", encoding="utf-8")
    sg.clear_marker(tmp_path)
    assert not sg.is_degraded(tmp_path)


# --- the three stages ------------------------------------------------------------------------------------------------

@pytest.fixture
def ep(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    return common.get_episode_dir(5)


def write_plan_inputs(ep):
    (ep / "script.json").write_text(json.dumps({"sections": []}), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps({"units": [{"id": 0}], "words": []}), encoding="utf-8")


def test_planning_that_recovers_on_the_second_attempt_writes_its_files(ep, monkeypatch, sleeps, alerts):
    write_plan_inputs(ep)
    calls = []

    def flaky(client, script, timings, style, limit):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("llm hiccup")
        return {"style": style, "beats": []}, {"beats": 0, "scenes": 0, "overlays": 0, "sections": []}

    monkeypatch.setattr(plan_shots, "make_client", lambda: object())
    monkeypatch.setattr(plan_shots, "plan_episode", flaky)
    assert plan_shots.main(5) == 0
    assert len(calls) == 2 and sleeps == [30]
    assert (ep / "shots.json").exists() and (ep / "plan_report.json").exists() and not sg.is_degraded(ep)


def test_planning_that_never_works_degrades_instead_of_failing_the_step(ep, monkeypatch, sleeps, alerts):
    write_plan_inputs(ep)
    monkeypatch.setattr(plan_shots, "make_client", lambda: object())
    monkeypatch.setattr(plan_shots, "plan_episode", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert plan_shots.main(5) == 0
    assert sg.read_marker(ep)["stage"] == "plan" and not (ep / "shots.json").exists()
    assert len(alerts) == 1


def test_planning_without_units_degrades_too(ep, sleeps, alerts):
    (ep / "script.json").write_text("{}", encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps({"sections": [], "total": 0}), encoding="utf-8")
    assert plan_shots.main(5) == 0
    assert sg.read_marker(ep)["stage"] == "plan" and "units" in sg.read_marker(ep)["error"]


def test_planning_in_strict_mode_raises(ep, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    (ep / "script.json").write_text("{}", encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps({"sections": [], "total": 0}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="units"):
        plan_shots.main(5)
    assert not sg.is_degraded(ep) and alerts == []


def test_a_fresh_planning_run_clears_the_marker_of_an_earlier_degraded_run(ep, monkeypatch, sleeps, alerts):
    write_plan_inputs(ep)
    sg.write_marker(ep, "plan", "old failure", 3)
    monkeypatch.setattr(plan_shots, "make_client", lambda: object())
    monkeypatch.setattr(plan_shots, "plan_episode",
                        lambda *a: ({"style": "x", "beats": []}, {"beats": 0, "scenes": 0, "overlays": 0, "sections": []}))
    assert plan_shots.main(5) == 0
    assert not sg.is_degraded(ep)


def test_planning_outside_shots_mode_does_nothing(ep, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    sg.write_marker(ep, "plan", "stale", 3)
    assert plan_shots.main(5) == 0
    assert sg.is_degraded(ep), "slideshow mode must not touch shots state"


@pytest.fixture
def image_stage(monkeypatch, ep):
    calls = {"slideshow": 0, "scenes": 0}
    (ep / "shots.json").write_text(json.dumps({"style": "ink", "scenes": []}), encoding="utf-8")
    monkeypatch.setattr(generate_images, "make_client", lambda timeout=0: object())
    monkeypatch.setattr(generate_images, "main_slideshow",
                        lambda eid, client, deployment: calls.__setitem__("slideshow", calls["slideshow"] + 1) or 0)
    return calls


def test_images_after_a_degraded_plan_take_the_legacy_per_section_path(ep, image_stage, sleeps, alerts):
    sg.write_marker(ep, "plan", "planning failed", 3)
    assert generate_images.main(5) == 0
    assert image_stage["slideshow"] == 1
    assert not (ep / "image_report.json").exists()


def break_scene_images(monkeypatch, image_stage):
    def broken(*args, **kwargs):
        image_stage["scenes"] += 1
        raise RuntimeError("image service down")

    monkeypatch.setattr(generate_images, "generate_scene_images", broken)


def test_images_that_fail_before_any_image_exists_fall_back_to_the_legacy_path(ep, image_stage, monkeypatch, sleeps, alerts):
    break_scene_images(monkeypatch, image_stage)
    assert generate_images.main(5) == 0
    assert image_stage["scenes"] == 3 and image_stage["slideshow"] == 1
    assert sg.read_marker(ep)["stage"] == "images" and len(alerts) == 1


def test_images_that_fail_after_some_scene_images_exist_keep_them_for_the_slideshow(ep, image_stage, monkeypatch, sleeps, alerts):
    (ep / "img_s1.png").write_bytes(b"png")
    break_scene_images(monkeypatch, image_stage)
    assert generate_images.main(5) == 0
    assert image_stage["scenes"] == 3 and image_stage["slideshow"] == 0
    assert sg.read_marker(ep)["stage"] == "images"


def test_a_legacy_section_image_does_not_count_as_a_scene_image(ep, image_stage, monkeypatch, sleeps, alerts):
    (ep / "img_01.png").write_bytes(b"png")
    break_scene_images(monkeypatch, image_stage)
    generate_images.main(5)
    assert image_stage["slideshow"] == 1


def test_images_that_recover_write_their_report(ep, image_stage, monkeypatch, sleeps, alerts):
    results = [RuntimeError("blip"), {"generated": 1, "fallback": 0, "skipped": 0, "scenes": [], "stopped_early": None}]

    def flaky(*args, **kwargs):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(generate_images, "generate_scene_images", flaky)
    assert generate_images.main(5) == 0
    assert (ep / "image_report.json").exists() and not sg.is_degraded(ep) and sleeps == [30]


def test_images_in_strict_mode_raise_after_the_retries(ep, image_stage, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    monkeypatch.setattr(generate_images, "generate_scene_images",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        generate_images.main(5)
    assert not sg.is_degraded(ep)


def test_a_slideshow_run_never_looks_at_the_marker(ep, image_stage, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("VIDEO_MODE", "slideshow")
    for name in ("is_degraded", "read_marker", "run_stage", "clear_marker", "write_marker"):
        monkeypatch.setattr(sg, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("guard consulted")))
    assert generate_images.main(5) == 0
    assert image_stage["slideshow"] == 1


@pytest.fixture
def ground_stage(ep, monkeypatch):
    (ep / "shots.json").write_text(json.dumps({"scenes": [], "beats": []}), encoding="utf-8")
    monkeypatch.setattr(ground_focus, "make_client", lambda: object())
    return ep


def test_grounding_is_skipped_when_the_episode_is_degraded(ground_stage, monkeypatch, sleeps, alerts):
    sg.write_marker(ground_stage, "images", "no images", 3)
    calls = []
    monkeypatch.setattr(ground_focus, "run", lambda *a: calls.append(a))
    assert ground_focus.main(5) == 0
    assert calls == [] and alerts == [] and sleeps == []


def test_grounding_failure_is_not_fatal(ground_stage, monkeypatch, sleeps, alerts):
    calls = []
    monkeypatch.setattr(ground_focus, "run", lambda *a: calls.append(1) or (_ for _ in ()).throw(RuntimeError("vision down")))
    assert ground_focus.main(5) == 0
    assert len(calls) == 3
    assert not sg.is_degraded(ground_stage) and len(alerts) == 1 and "「聚焦」" in alerts[0]
    assert not (ground_stage / "ground_report.json").exists()


def test_grounding_retries_then_succeeds(ground_stage, monkeypatch, sleeps, alerts):
    results = [RuntimeError("blip"), {"grounded": 0, "missing": []}]

    def flaky(*args):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ground_focus, "run", flaky)
    assert ground_focus.main(5) == 0
    assert json.loads((ground_stage / "ground_report.json").read_text(encoding="utf-8")) == {"grounded": 0, "missing": []}
    assert alerts == []


def test_grounding_in_strict_mode_raises(ground_stage, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    monkeypatch.setattr(ground_focus, "run", lambda *a: (_ for _ in ()).throw(RuntimeError("vision down")))
    with pytest.raises(RuntimeError):
        ground_focus.main(5)


# --- make_video ------------------------------------------------------------------------------------------------------

@pytest.fixture
def video(ep, monkeypatch):
    calls = {"render": 0, "slideshow": 0, "inputs": 0}
    def render(eid):
        calls["render"] += 1
        (ep / "final.mp4").write_bytes(b"video")
        return 0

    monkeypatch.setattr(make_video.render_video, "main", render)
    monkeypatch.setattr(make_video.build_video, "main", lambda eid: calls.__setitem__("slideshow", calls["slideshow"] + 1) or 0)
    monkeypatch.setattr(make_video, "make_slideshow_inputs", lambda d: calls.__setitem__("inputs", calls["inputs"] + 1) or [])
    monkeypatch.setattr(make_video, "notify_discord", lambda message: calls.setdefault("alerts", []).append(message))
    return calls


def test_a_degraded_episode_skips_the_renderer_and_builds_the_slideshow(ep, video):
    sg.write_marker(ep, "plan", "planning failed", 3)
    assert make_video.main(5) == 0
    assert video["render"] == 0 and video["slideshow"] == 1 and video["inputs"] == 1


def test_the_degraded_slideshow_does_not_repeat_the_alert_the_stage_already_sent(ep, video):
    sg.write_marker(ep, "images", "image service down", 3)
    make_video.main(5)
    assert "alerts" not in video


def test_a_degraded_episode_does_not_need_shots_json(ep, video):
    assert not (ep / "shots.json").exists()
    sg.write_marker(ep, "plan", "planning failed", 3)
    assert make_video.main(5) == 0


def test_stale_render_outputs_are_removed_when_degrading_because_of_a_marker(ep, video):
    (ep / "final.mp4").write_bytes(b"old")
    (ep / "video_report.json").write_text("{}", encoding="utf-8")
    sg.write_marker(ep, "images", "down", 3)
    make_video.main(5)
    assert not (ep / "final.mp4").exists() and not (ep / "video_report.json").exists()


def test_without_a_marker_the_renderer_still_runs(ep, video):
    (ep / "shots.json").write_text("{}", encoding="utf-8")
    assert make_video.main(5) == 0
    assert video["render"] == 1 and video["slideshow"] == 0


# --- notify ----------------------------------------------------------------------------------------------------------

def test_the_run_summary_names_the_degraded_stage(tmp_path):
    sg.write_marker(tmp_path, "plan", "planning failed", 3)
    assert notify.summarize_run(tmp_path).startswith("分鏡降級 (規劃)")
    sg.write_marker(tmp_path, "images", "image service down", 3)
    assert notify.summarize_run(tmp_path).startswith("分鏡降級 (產圖)")


def test_the_degrade_part_sits_in_front_of_the_other_parts(tmp_path):
    sg.write_marker(tmp_path, "images", "down", 3)
    (tmp_path / "image_report.json").write_text(json.dumps({"generated": 2, "fallback": 1, "skipped": 0}), encoding="utf-8")
    summary = notify.summarize_run(tmp_path)
    assert summary.split("；")[0] == "分鏡降級 (產圖)" and "圖片 3 張" in summary


def test_an_unknown_degraded_stage_is_still_reported(tmp_path):
    (tmp_path / sg.MARKER).write_text("{not json", encoding="utf-8")
    assert notify.summarize_run(tmp_path) == "分鏡降級"


def test_a_healthy_run_has_no_degrade_part(tmp_path):
    (tmp_path / "image_report.json").write_text(json.dumps({"generated": 2, "fallback": 0, "skipped": 0}), encoding="utf-8")
    assert "降級" not in notify.summarize_run(tmp_path)


# --- local runner ----------------------------------------------------------------------------------------------------

@pytest.fixture
def runner(monkeypatch):
    seen = []
    monkeypatch.setattr(local, "run_stage", lambda name, eid, limit: seen.append((name, os.environ.get("SHOTS_STRICT"))))
    for key in ("TOPIC_ID", "VIDEO_MODE", "VISUAL_STYLE", "SHOTS_STRICT", "DISCORD_WEBHOOK_URL", *local.SHORT_MODE_ENV):
        monkeypatch.setenv(key, "placeholder")   # so that monkeypatch records the key and removes whatever main() sets
        monkeypatch.delenv(key)
    return seen


def test_the_local_runner_is_strict_by_default(runner, monkeypatch):
    assert local.main(["--topic-id", "3"]) == 0
    assert {strict for _, strict in runner} == {"1"}


def test_the_local_runner_lets_you_turn_strict_mode_off(runner, monkeypatch):
    monkeypatch.setenv("SHOTS_STRICT", "0")
    assert local.main(["--topic-id", "3"]) == 0
    assert {strict for _, strict in runner} == {"0"}


# --- time awareness: slow failures are not retried -------------------------------------------------------------------

def advancing_clock(monkeypatch, step):
    """Every call of the guard's monotonic clock moves `step` seconds, so each attempt measures exactly `step`."""
    state = {"now": 0.0}

    def clock():
        state["now"] += step
        return state["now"]

    monkeypatch.setattr(sg, "_monotonic", clock)


def test_a_failure_that_took_too_long_is_not_retried(tmp_path, monkeypatch, sleeps, alerts):
    advancing_clock(monkeypatch, 700)
    fn = failing(99)
    assert sg.run_stage("plan", 7, tmp_path, fn) is False
    assert fn.state["calls"] == 1 and sleeps == []
    assert sg.read_marker(tmp_path)["attempts"] == 1 and "嘗試 1 次" in alerts[0]


@pytest.mark.parametrize("step,calls", [(599, 3), (599.9, 3), (600, 1), (601, 1)])
def test_the_slow_failure_boundary_is_ten_minutes(tmp_path, monkeypatch, sleeps, alerts, step, calls):
    advancing_clock(monkeypatch, step)
    fn = failing(99)
    sg.run_stage("plan", 7, tmp_path, fn)
    assert fn.state["calls"] == calls


def test_a_timeout_is_never_retried_even_when_it_was_quick(tmp_path, monkeypatch, sleeps, alerts):
    advancing_clock(monkeypatch, 1)
    fn = failing(99, error=sg.StageTimeout("超過 5 秒仍未完成"))
    assert sg.run_stage("images", 7, tmp_path, fn) is False
    assert fn.state["calls"] == 1 and sleeps == []
    assert "StageTimeout" in sg.read_marker(tmp_path)["error"]


def test_a_plain_timeout_error_from_the_stage_is_still_retried(tmp_path, monkeypatch, sleeps, alerts):
    advancing_clock(monkeypatch, 1)
    fn = failing(99, error=TimeoutError("socket timed out"))
    sg.run_stage("plan", 7, tmp_path, fn)
    assert fn.state["calls"] == 3, "only the guard's own StageTimeout (the stage ran out of time) skips the retries"


def test_a_slow_failure_in_strict_mode_raises_after_one_attempt(tmp_path, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STRICT", "1")
    advancing_clock(monkeypatch, 700)
    fn = failing(99)
    with pytest.raises(RuntimeError):
        sg.run_stage("plan", 7, tmp_path, fn)
    assert fn.state["calls"] == 1 and sleeps == []


def test_a_slow_success_is_a_success(tmp_path, monkeypatch, sleeps, alerts):
    advancing_clock(monkeypatch, 5000)
    assert sg.run_stage("plan", 7, tmp_path, failing(0)) is True and alerts == []


def test_the_pauses_keep_doubling_with_more_attempts(tmp_path, monkeypatch, sleeps, alerts):
    monkeypatch.setenv("SHOTS_STAGE_ATTEMPTS", "5")
    sg.run_stage("plan", 7, tmp_path, failing(99))
    assert sleeps == [30, 60, 120, 240]


# --- per-attempt deadline --------------------------------------------------------------------------------------------

class FakeSignal:
    SIGALRM, ITIMER_REAL, SIG_DFL = 14, 0, 0

    def __init__(self):
        self.timer, self.handler = [], None

    def signal(self, number, handler):
        previous, self.handler = self.handler, handler
        return previous

    def setitimer(self, which, seconds):
        self.timer.append((which, seconds))


@pytest.fixture
def fake_signal(monkeypatch):
    fake = FakeSignal()
    monkeypatch.setattr(sg, "signal", fake)
    return fake


def test_each_attempt_arms_the_timer_and_always_disarms_it(tmp_path, fake_signal, sleeps, alerts):
    seen = []

    def fn():
        seen.append((list(fake_signal.timer), fake_signal.handler is not None))
        if len(seen) == 1:
            raise RuntimeError("first attempt fails")

    assert sg.run_stage("plan", 7, tmp_path, fn) is True
    assert seen[0] == ([(0, 1800)], True)
    assert seen[1] == ([(0, 1800), (0, 0), (0, 1800)], True)
    assert fake_signal.timer == [(0, 1800), (0, 0), (0, 1800), (0, 0)]
    assert fake_signal.handler == fake_signal.SIG_DFL, "the previous handler is restored"


def test_the_alarm_handler_raises_a_stage_timeout(tmp_path, fake_signal, sleeps, alerts):
    captured = {}

    def fn():
        captured["handler"] = fake_signal.handler

    sg.run_stage("ground", 7, tmp_path, fn, fatal=False)
    with pytest.raises(sg.StageTimeout, match="900"):
        captured["handler"](fake_signal.SIGALRM, None)


@pytest.mark.parametrize("stage,seconds", [("plan", 1800), ("images", 5400), ("ground", 900)])
def test_every_stage_has_its_own_default_limit(tmp_path, fake_signal, sleeps, alerts, stage, seconds):
    sg.run_stage(stage, 7, tmp_path, failing(0))
    assert fake_signal.timer[0] == (0, seconds) and sg.STAGE_MAX_SEC[stage] == seconds


def test_an_explicit_limit_wins_and_an_unknown_stage_has_none(tmp_path, fake_signal, sleeps, alerts):
    sg.run_stage("plan", 7, tmp_path, failing(0), max_sec=12)
    assert fake_signal.timer == [(0, 12), (0, 0)]
    fake_signal.timer.clear()
    sg.run_stage("render", 7, tmp_path, failing(0))
    assert fake_signal.timer == []


def test_an_environment_without_setitimer_just_runs_without_a_limit(tmp_path, monkeypatch, sleeps, alerts):
    class Windows:
        SIGALRM = 14

    monkeypatch.setattr(sg, "signal", Windows())
    assert sg.run_stage("plan", 7, tmp_path, failing(0)) is True


def test_no_limit_is_armed_off_the_main_thread(tmp_path, fake_signal, sleeps, alerts):
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(sg.run_stage("plan", 7, tmp_path, failing(0))))
    worker.start()
    worker.join()
    assert outcome == [True] and fake_signal.timer == []


def test_a_stage_that_hangs_is_interrupted_for_real(tmp_path, monkeypatch, alerts):
    if not hasattr(signal, "setitimer"):
        # Windows only: GitHub Actions runs Linux, and the CI guard (tests/conftest.py) fails the session on this skip.
        pytest.skip("SIGALRM is not available")
    monkeypatch.setenv("SHOTS_STAGE_ATTEMPTS", "3")
    started = real_time.monotonic()
    assert sg.run_stage("plan", 7, tmp_path, lambda: real_time.sleep(30), max_sec=0.3) is False
    assert real_time.monotonic() - started < 10
    marker = sg.read_marker(tmp_path)
    assert marker["stage"] == "plan" and marker["attempts"] == 1 and "StageTimeout" in marker["error"]
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0, "the timer must be disarmed afterwards"


# --- log, markers and messages ---------------------------------------------------------------------------------------

def test_every_failed_attempt_is_logged_with_its_traceback(tmp_path, sleeps, alerts, caplog):
    with caplog.at_level(logging.WARNING, logger="shots_guard"):
        sg.run_stage("plan", 7, tmp_path, failing(2, error=KeyError("image")))
    attempts_logged = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(attempts_logged) == 2
    assert all(r.exc_info and r.exc_info[0] is KeyError for r in attempts_logged)


def test_a_whitespace_only_message_still_names_the_exception_type(tmp_path, sleeps, alerts):
    sg.run_stage("plan", 7, tmp_path, failing(99, error=RuntimeError("   ")))
    assert sg.read_marker(tmp_path)["error"] == "RuntimeError"


def test_a_long_reason_is_cut_to_exactly_five_hundred_characters(tmp_path, sleeps, alerts):
    sg.run_stage("plan", 7, tmp_path, failing(99, error=RuntimeError("x" * 3000)))
    error = sg.read_marker(tmp_path)["error"]
    assert len(error) == 500 and error.endswith("…")


def test_chinese_text_stays_readable_in_the_marker_file(tmp_path, sleeps, alerts):
    sg.run_stage("images", 7, tmp_path, failing(99, error=RuntimeError("圖片服務沒有回應")))
    assert "圖片服務沒有回應" in (tmp_path / sg.MARKER).read_text(encoding="utf-8")


def test_a_marker_that_is_a_directory_counts_as_degraded(tmp_path):
    (tmp_path / sg.MARKER).mkdir()
    assert sg.read_marker(tmp_path) == {"stage": "unknown"}


def test_a_marker_nested_too_deeply_counts_as_degraded(tmp_path):
    (tmp_path / sg.MARKER).write_text("[" * 200000, encoding="utf-8")
    assert sg.read_marker(tmp_path) == {"stage": "unknown"}


def test_the_summary_survives_a_failing_marker_reader(tmp_path, monkeypatch):
    (tmp_path / "image_report.json").write_text(json.dumps({"generated": 2, "fallback": 0, "skipped": 0}), encoding="utf-8")
    monkeypatch.setattr(notify.shots_guard, "read_marker", lambda ep_dir: (_ for _ in ()).throw(OSError("denied")))
    assert "圖片 2 張" in notify.summarize_run(tmp_path)


def test_the_stage_names_are_shared_by_the_alert_and_the_summary():
    assert sg.STAGE_NAMES == {"plan": "規劃", "images": "產圖", "ground": "聚焦", "render": "渲染"}
    assert notify.DEGRADED_STAGES == {"plan": "規劃", "images": "產圖", "render": "渲染"}


# --- a render failure leaves a marker too ----------------------------------------------------------------------------

def test_a_render_failure_is_recorded_in_the_marker_and_the_summary(ep, video, monkeypatch):
    (ep / "shots.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(make_video.render_video, "main", lambda eid: (_ for _ in ()).throw(RuntimeError("ffmpeg died")))
    assert make_video.main(5) == 0
    marker = sg.read_marker(ep)
    assert marker["stage"] == "render" and "ffmpeg died" in marker["error"]
    assert video["slideshow"] == 1 and len(video["alerts"]) == 1
    assert notify.summarize_run(ep).startswith("分鏡降級 (渲染)")


def test_the_marker_of_an_earlier_render_failure_does_not_block_a_new_render(ep, video):
    (ep / "shots.json").write_text("{}", encoding="utf-8")
    sg.write_marker(ep, "render", "RuntimeError: old", 1)
    assert make_video.main(5) == 0
    assert video["render"] == 1 and video["slideshow"] == 0 and not sg.is_degraded(ep)


def test_an_unreadable_marker_still_degrades_the_video(ep, video):
    (ep / sg.MARKER).write_text("{not json", encoding="utf-8")
    assert make_video.main(5) == 0
    assert video["render"] == 0 and video["slideshow"] == 1
