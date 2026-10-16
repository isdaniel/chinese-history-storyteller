import inspect
import io
import json
import logging
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import common
import generate_images as gi
from fakes import BadRequest, FakeImageClient, FakeImages


class ServerError(Exception):
    status_code = 500


class RecordingImages(FakeImages):
    def __init__(self, outcomes):
        super().__init__(outcomes)
        self.kwargs = []

    def generate(self, **kwargs):
        self.kwargs.append(kwargs)
        return super().generate(**kwargs)


class RecordingImageClient:
    def __init__(self, outcomes):
        self.images = RecordingImages(outcomes)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    recorded = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def sleeps(no_real_sleep):
    return no_real_sleep


@pytest.fixture
def episode_env(monkeypatch, tmp_path):
    for key in ("VIDEO_MODE", "VISUAL_STYLE", "IMAGE_THROTTLE_SEC", "IMAGE_STAGE_MAX_SEC",
                "AZURE_OPENAI_IMAGE_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)


def patch_client(monkeypatch, client):
    seen = {}

    def fake_make_client(timeout=300.0):
        seen["timeout"] = timeout
        return client

    monkeypatch.setattr(gi, "make_client", fake_make_client)
    return seen


def pin_jitter_to_upper_bound(monkeypatch):
    bounds = []

    def uniform(low, high):
        bounds.append((low, high))
        return high

    monkeypatch.setattr(gi.random, "uniform", uniform)
    return bounds


def write_shots(ep_dir, **overrides):
    (ep_dir / "shots.json").write_text(json.dumps(dict(SHOTS, **overrides)), encoding="utf-8")


def write_script(ep_dir, sections=2):
    script = {"title": "t", "sections": [
        {"section_id": k, "narration": "n", "image_prompt": f"sec{k}"} for k in range(1, sections + 1)]}
    (ep_dir / "script.json").write_text(json.dumps(script), encoding="utf-8")


def read_report(ep_dir):
    return json.loads((ep_dir / "image_report.json").read_text(encoding="utf-8"))


def png(color=(10, 20, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


SHOTS = {"style": "ink", "scenes": [
    {"id": "s1", "image_prompt": "one", "image": "img_s1.png"},
    {"id": "s2", "image_prompt": "two", "image": "img_s2.png"},
    {"id": "s3", "image_prompt": "three", "image": "img_s3.png"}]}


def run(tmp_path, outcomes, style="ink", sleeps=None, **kw):
    client = FakeImageClient(outcomes)
    calls = sleeps if sleeps is not None else []
    report = gi.generate_scene_images(client, "dep", SHOTS, tmp_path, style, sleep=lambda: calls.append(1), **kw)
    return client, report, calls


def test_generates_one_image_per_scene_with_the_style_suffix(tmp_path):
    client, report, sleeps = run(tmp_path, [png((1, 1, 1)), png((2, 2, 2)), png((3, 3, 3))])
    assert (report["generated"], report["fallback"], report["skipped"]) == (3, 0, 0)
    assert all((tmp_path / f"img_s{k}.png").exists() for k in (1, 2, 3))
    assert client.images.prompts[0].startswith("one") and "ink painting" in client.images.prompts[0]
    assert len(sleeps) == 2, "throttle between images but not after the last one"


def test_cinematic_style_changes_the_prompt(tmp_path):
    client, _, _ = run(tmp_path, [png(), png(), png()], style="cinematic")
    assert "cinematic realistic" in client.images.prompts[1]


def test_existing_images_are_skipped(tmp_path):
    (tmp_path / "img_s1.png").write_bytes(png((9, 9, 9)))
    client, report, _ = run(tmp_path, [png(), png()])
    assert report["skipped"] == 1 and len(client.images.prompts) == 2


def test_failed_scene_reuses_the_previous_image(tmp_path):
    first = png((5, 5, 5))
    _, report, _ = run(tmp_path, [first, BadRequest("blocked"), png((6, 6, 6))])
    assert report["fallback"] == 1 and report["generated"] == 2
    assert (tmp_path / "img_s2.png").read_bytes() == first
    assert [s["status"] for s in report["scenes"]] == ["generated", "fallback", "generated"]


def test_first_scene_failure_writes_the_fallback_image_instead_of_crashing(tmp_path):
    written = []

    def fake_fallback(path):
        written.append(path.name)
        path.write_bytes(png((7, 7, 7)))

    _, report, _ = run(tmp_path, [BadRequest("blocked"), png(), png()], write_fallback=fake_fallback)
    assert written == ["img_s1.png"] and report["fallback"] == 1
    assert (tmp_path / "img_s1.png").exists()


def test_every_scene_failing_still_leaves_every_image_in_place(tmp_path):
    _, report, _ = run(tmp_path, [BadRequest("x")] * 3, write_fallback=lambda p: p.write_bytes(png()))
    assert report["fallback"] == 3
    assert all((tmp_path / f"img_s{k}.png").exists() for k in (1, 2, 3))


def test_skipped_scene_is_reported_and_reused_as_previous(tmp_path):
    existing = png((9, 9, 9))
    (tmp_path / "img_s1.png").write_bytes(existing)
    _, report, _ = run(tmp_path, [BadRequest("x"), png((2, 2, 2))])
    assert [(s["scene"], s["status"]) for s in report["scenes"]] == [
        ("s1", "skipped"), ("s2", "fallback"), ("s3", "generated")]
    assert (tmp_path / "img_s2.png").read_bytes() == existing
    assert (report["generated"], report["fallback"], report["skipped"]) == (1, 1, 1)


def test_skipped_scene_is_not_throttled(tmp_path):
    (tmp_path / "img_s1.png").write_bytes(png((9, 9, 9)))
    _, report, calls = run(tmp_path, [png(), png()])
    assert report["skipped"] == 1 and len(calls) == 1


def test_failed_scenes_use_write_fallback_once_then_copy(tmp_path):
    written = []

    def fake_fallback(path):
        written.append(path.name)
        path.write_bytes(png((7, 7, 7)))

    run(tmp_path, [BadRequest("x")] * 3, write_fallback=fake_fallback)
    assert written == ["img_s1.png"]
    assert (tmp_path / "img_s3.png").read_bytes() == png((7, 7, 7))


def test_image_request_parameters():
    client = RecordingImageClient([png((1, 2, 3))])
    assert gi.generate_image_bytes(client, "my-dep", "a prompt", "s1") == png((1, 2, 3))
    assert client.images.kwargs == [dict(model="my-dep", prompt="a prompt", n=1, size="1536x1024", quality="medium")]


def test_transient_errors_are_retried_three_times_with_backoff(monkeypatch, sleeps):
    monkeypatch.setattr(gi.random, "uniform", lambda low, high: high)
    client = FakeImageClient([ServerError("a"), ServerError("b"), png()])
    assert gi.generate_image_bytes(client, "dep", "p", "s1") == png()
    assert len(client.images.prompts) == 3
    assert sleeps == [60 + 15, 120 + 15]


def test_gives_up_after_three_attempts(sleeps):
    client = FakeImageClient([ServerError("a")] * 5)
    with pytest.raises(ServerError):
        gi.generate_image_bytes(client, "dep", "p", "s1")
    assert len(client.images.prompts) == 3


def test_a_400_is_not_retried(sleeps):
    client = FakeImageClient([BadRequest("blocked"), png()])
    with pytest.raises(BadRequest):
        gi.generate_image_bytes(client, "dep", "p", "s1")
    assert len(client.images.prompts) == 1 and sleeps == []


def test_placeholder_png_is_a_valid_1536x1024_image(tmp_path):
    out = tmp_path / "placeholder.png"
    gi._write_placeholder_png(out)
    with Image.open(out) as image:
        image.load()
        assert image.size == (1536, 1024) and image.mode == "RGB"
        assert image.getpixel((0, 0)) == (60, 50, 40) and image.getpixel((1535, 1023)) == (60, 50, 40)


def test_write_fallback_prefers_the_asset_then_the_placeholder(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(gi, "__file__", str(tmp_path / "scripts" / "generate_images.py"))
    out = tmp_path / "out.png"
    gi._write_fallback(out)
    with Image.open(out) as image:
        assert image.size == (1536, 1024)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "fallback.png").write_bytes(png((3, 3, 3)))
    gi._write_fallback(out)
    assert out.read_bytes() == png((3, 3, 3))


def test_main_shots_mode_uses_the_shots_style_and_writes_the_report(monkeypatch, episode_env, sleeps):
    ep_dir = common.get_episode_dir(5)
    write_shots(ep_dir, style="cinematic")
    monkeypatch.setenv("VIDEO_MODE", "shots")
    monkeypatch.setenv("VISUAL_STYLE", "ink")
    client = RecordingImageClient([png(), png(), png()])
    seen = patch_client(monkeypatch, client)
    assert gi.main(5) == 0
    assert seen["timeout"] == 180.0
    assert all("cinematic realistic" in prompt for prompt in client.images.prompts)
    report = read_report(ep_dir)
    assert report["generated"] == 3 and [s["scene"] for s in report["scenes"]] == ["s1", "s2", "s3"]
    assert (ep_dir / "img_s1.png").exists() and not (ep_dir / "img_01.png").exists()


@pytest.mark.parametrize("configured,expected", [(None, 30 + 10), ("5", 5 + 10)])
def test_main_throttle_defaults_to_30_seconds_plus_jitter_and_can_be_overridden(
        monkeypatch, episode_env, sleeps, configured, expected):
    ep_dir = common.get_episode_dir(6)
    write_shots(ep_dir)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    if configured is not None:
        monkeypatch.setenv("IMAGE_THROTTLE_SEC", configured)
    bounds = pin_jitter_to_upper_bound(monkeypatch)
    patch_client(monkeypatch, RecordingImageClient([png(), png(), png()]))
    gi.main(6)
    assert sleeps == [expected, expected] and bounds == [(0, 10), (0, 10)]


@pytest.mark.parametrize("mode", ["shots", "slideshow"])
@pytest.mark.parametrize("configured,expected", [(None, "gpt-image-2"), ("my-img", "my-img")])
def test_main_deployment_comes_from_the_environment(monkeypatch, episode_env, sleeps, mode, configured, expected):
    ep_dir = common.get_episode_dir(7)
    write_shots(ep_dir)
    write_script(ep_dir, sections=3)
    monkeypatch.setenv("VIDEO_MODE", mode)
    if configured is not None:
        monkeypatch.setenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", configured)
    client = RecordingImageClient([png(), png(), png()])
    patch_client(monkeypatch, client)
    assert gi.main(7) == 0
    assert [call["model"] for call in client.images.kwargs] == [expected] * 3


def test_main_defaults_to_the_slideshow_with_one_image_per_section(monkeypatch, episode_env, sleeps):
    ep_dir = common.get_episode_dir(8)
    write_script(ep_dir)
    bounds = pin_jitter_to_upper_bound(monkeypatch)
    client = RecordingImageClient([png(), png()])
    seen = patch_client(monkeypatch, client)
    assert gi.main(8) == 0
    assert seen["timeout"] == 180.0
    assert (ep_dir / "img_01.png").exists() and (ep_dir / "img_02.png").exists()
    assert not (ep_dir / "image_report.json").exists() and not (ep_dir / "img_s1.png").exists()
    assert client.images.prompts[0].startswith("sec1") and "cinematic realistic" in client.images.prompts[0]
    assert sleeps == [40, 40] and bounds == [(0, 10), (0, 10)]


@pytest.mark.parametrize("style,phrase", [("ink", "ink painting"), ("cinematic", "cinematic realistic")])
def test_slideshow_uses_the_visual_style_from_the_environment(monkeypatch, episode_env, sleeps, style, phrase):
    write_script(common.get_episode_dir(9), sections=1)
    monkeypatch.setenv("VISUAL_STYLE", style)
    client = RecordingImageClient([png()])
    patch_client(monkeypatch, client)
    gi.main(9)
    assert phrase in client.images.prompts[0]


def test_slideshow_skips_existing_images_without_sleeping(monkeypatch, episode_env, sleeps):
    ep_dir = common.get_episode_dir(10)
    write_script(ep_dir)
    (ep_dir / "img_01.png").write_bytes(png((9, 9, 9)))
    client = RecordingImageClient([png()])
    patch_client(monkeypatch, client)
    assert gi.main(10) == 0
    assert len(client.images.kwargs) == 1 and client.images.prompts[0].startswith("sec2")
    assert (ep_dir / "img_01.png").read_bytes() == png((9, 9, 9))
    assert len(sleeps) == 1


def test_slideshow_failure_writes_the_fallback_and_raises_the_alert(monkeypatch, episode_env, sleeps, caplog):
    ep_dir = common.get_episode_dir(11)
    write_script(ep_dir)
    patch_client(monkeypatch, RecordingImageClient([BadRequest("blocked"), png()]))
    written = []
    monkeypatch.setattr(gi, "_write_fallback", lambda path: (written.append(path.name), path.write_bytes(png())))
    with caplog.at_level(logging.INFO):
        assert gi.main(11) == 0
    assert written == ["img_01.png"]
    assert any("[FALLBACK ALERT] 1/2" in record.getMessage() for record in caplog.records)


def test_slideshow_without_failures_raises_no_alert(monkeypatch, episode_env, sleeps, caplog):
    write_script(common.get_episode_dir(12))
    patch_client(monkeypatch, RecordingImageClient([png(), png()]))
    with caplog.at_level(logging.INFO):
        gi.main(12)
    assert not any("FALLBACK ALERT" in record.getMessage() for record in caplog.records)


def test_rerun_regenerates_scenes_that_fell_back_last_time(monkeypatch, episode_env, sleeps, caplog):
    ep_dir = common.get_episode_dir(14)
    write_shots(ep_dir)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    patch_client(monkeypatch, RecordingImageClient([BadRequest("blocked"), png(), png()]))
    gi.main(14)
    assert read_report(ep_dir)["scenes"][0]["status"] == "fallback"

    retried = png((4, 4, 4))
    patch_client(monkeypatch, RecordingImageClient([retried]))
    with caplog.at_level(logging.INFO):
        assert gi.main(14) == 0
    report = read_report(ep_dir)
    assert [s["status"] for s in report["scenes"]] == ["generated", "skipped", "skipped"]
    assert (report["generated"], report["fallback"], report["skipped"]) == (1, 0, 2)
    assert (ep_dir / "img_s1.png").read_bytes() == retried
    assert any("重新生成" in record.getMessage() for record in caplog.records)


def test_discard_fallback_images_removes_only_the_scenes_that_fell_back(tmp_path):
    for k in (1, 2, 3):
        (tmp_path / f"img_s{k}.png").write_bytes(png((k, k, k)))
    (tmp_path / "image_report.json").write_text(json.dumps({"scenes": [
        {"scene": "s1", "status": "generated"}, {"scene": "s2", "status": "fallback"},
        {"scene": "s3", "status": "skipped"}, {"scene": "gone", "status": "fallback"}]}), encoding="utf-8")
    assert gi.discard_fallback_images(SHOTS, tmp_path) == ["s2"]
    assert [(tmp_path / f"img_s{k}.png").exists() for k in (1, 2, 3)] == [True, False, True]


def test_discard_fallback_images_tolerates_an_image_that_is_already_gone(tmp_path):
    (tmp_path / "image_report.json").write_text(
        json.dumps({"scenes": [{"scene": "s1", "status": "fallback"}]}), encoding="utf-8")
    assert gi.discard_fallback_images(SHOTS, tmp_path) == ["s1"]


@pytest.mark.parametrize("content", [
    None, "not json {", "[]", "null", "{}", '{"scenes": 5}', '{"scenes": ["x"]}',
    '{"scenes": [{"scene": "s1"}]}',
    '{"scenes": [{"scene": "s1", "status": "fallback"}, "x"]}'])
def test_discard_fallback_images_ignores_a_missing_or_malformed_report(tmp_path, content):
    for k in (1, 2, 3):
        (tmp_path / f"img_s{k}.png").write_bytes(png())
    if content is not None:
        (tmp_path / "image_report.json").write_text(content, encoding="utf-8")
    assert gi.discard_fallback_images(SHOTS, tmp_path) == []
    assert all((tmp_path / f"img_s{k}.png").exists() for k in (1, 2, 3))


# --- circuit breaker and stage time limit (shots path only) -----------------


class Unavailable(Exception):
    status_code = 503


class RateLimited(Exception):
    status_code = 429


class ConnectionLost(Exception):
    """No status code at all, like a dropped connection or a timeout."""


class ProxyError(Exception):
    """Status only on .response, the way the SDK's APIStatusError exposes it."""

    def __init__(self, status):
        super().__init__(status)
        self.response = SimpleNamespace(status_code=status)


class Statused(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status_code = status


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class ClockedImages(FakeImages):
    def __init__(self, outcomes, clock, step):
        super().__init__(outcomes)
        self.clock, self.step = clock, step

    def generate(self, **kwargs):
        self.clock.now += self.step
        return super().generate(**kwargs)


class ClockedClient:
    def __init__(self, outcomes, clock, step):
        self.images = ClockedImages(outcomes, clock, step)


def many_shots(count):
    return {"style": "ink", "scenes": [
        {"id": f"s{k}", "image_prompt": f"p{k}", "image": f"img_s{k}.png"} for k in range(1, count + 1)]}


def expand(outcomes):
    """One entry per scene: a 400 is attempted once, anything else three times (with_backoff's limit)."""
    queue = []
    for outcome in outcomes:
        queue += [outcome] * (3 if isinstance(outcome, Exception) and not isinstance(outcome, BadRequest) else 1)
    return queue


def run_many(tmp_path, count, outcomes, client=None, **kw):
    client = client or FakeImageClient(expand(outcomes))
    throttles = []
    report = gi.generate_scene_images(client, "dep", many_shots(count), tmp_path, "ink",
                                      sleep=lambda: throttles.append(1), **kw)
    return client, report, throttles


def statuses(report):
    return [entry["status"] for entry in report["scenes"]]


def breaker_errors(caplog):
    return [record for record in caplog.records
            if record.levelno == logging.ERROR and "不再呼叫圖片服務" in record.getMessage()]


def test_the_failure_limit_is_three():
    assert gi.MAX_CONSECUTIVE_FAILURES == 3
    assert gi.DEFAULT_IMAGE_STAGE_MAX_SEC == 4500


def test_a_normal_run_reports_that_it_did_not_stop_early(tmp_path):
    _, report, _ = run_many(tmp_path, 2, [png(), png()])
    assert report == {"generated": 2, "fallback": 0, "skipped": 0, "stopped_early": None,
                      "scenes": [{"scene": "s1", "status": "generated"}, {"scene": "s2", "status": "generated"}]}


def test_two_failures_in_a_row_do_not_trip_and_a_success_restarts_the_count(tmp_path):
    outcomes = [ServerError("a"), ServerError("b"), png(), ServerError("c"), ServerError("d"), png(), png()]
    client, report, _ = run_many(tmp_path, 7, outcomes)
    assert statuses(report) == ["fallback", "fallback", "generated", "fallback", "fallback", "generated", "generated"]
    assert len(client.images.prompts) == 3 + 3 + 1 + 3 + 3 + 1 + 1
    assert report["stopped_early"] is None


def test_three_failures_in_a_row_stop_the_service_calls_for_the_remaining_scenes(tmp_path, sleeps):
    first = png((5, 5, 5))
    client, report, throttles = run_many(tmp_path, 6, [first, ServerError("a"), ServerError("b"), ServerError("c"),
                                                        png(), png()])
    assert len(client.images.prompts) == 1 + 3 + 3 + 3, "the 5th and 6th scenes must not call the service"
    assert statuses(report) == ["generated", "fallback", "fallback", "fallback", "fallback", "fallback"]
    assert (report["generated"], report["fallback"], report["skipped"]) == (1, 5, 0)
    assert report["stopped_early"] == "consecutive_failures"
    assert all((tmp_path / f"img_s{k}.png").read_bytes() == first for k in range(2, 7))
    assert len(throttles) == 3, "throttle after scenes 1-3 only; none once the breaker is open"
    assert len(sleeps) == 6, "only the backoff sleeps of the three failing scenes"


def test_scenes_after_the_trip_use_the_placeholder_when_nothing_was_generated(tmp_path):
    written = []

    def fake_fallback(path):
        written.append(path.name)
        path.write_bytes(png((7, 7, 7)))

    client, report, _ = run_many(tmp_path, 5, [ServerError("a")] * 3 + [png()] * 2, write_fallback=fake_fallback)
    assert written == ["img_s1.png"]
    assert all((tmp_path / f"img_s{k}.png").read_bytes() == png((7, 7, 7)) for k in range(1, 6))
    assert len(client.images.prompts) == 9 and statuses(report) == ["fallback"] * 5
    assert report["fallback"] == 5 and report["stopped_early"] == "consecutive_failures"


def test_the_breaker_logs_exactly_one_error_with_the_count(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        run_many(tmp_path, 8, [ServerError("a")] * 3 + [png()] * 5)
    [record] = breaker_errors(caplog)
    assert "連續 3 張" in record.getMessage() and "備援圖" in record.getMessage()


def test_a_trip_on_the_last_scene_has_nothing_left_to_stop(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        client, report, _ = run_many(tmp_path, 3, [ServerError("a")] * 3)
    assert statuses(report) == ["fallback"] * 3 and len(client.images.prompts) == 9
    assert report["stopped_early"] is None and breaker_errors(caplog) == []


def test_a_bad_request_between_failures_resets_the_streak(tmp_path):
    outcomes = [ServerError("a"), ServerError("b"), BadRequest("blocked"), ServerError("c"), ServerError("d"),
                png(), png()]
    client, report, _ = run_many(tmp_path, 7, outcomes)
    assert len(client.images.prompts) == 3 + 3 + 1 + 3 + 3 + 1 + 1
    assert statuses(report) == ["fallback"] * 5 + ["generated"] * 2
    assert report["stopped_early"] is None


def test_bad_requests_alone_never_trip_the_breaker(tmp_path):
    client, report, throttles = run_many(tmp_path, 10, [BadRequest("blocked")] * 10)
    assert len(client.images.prompts) == 10 and report["fallback"] == 10
    assert report["stopped_early"] is None and len(throttles) == 9


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 499, 600])
def test_statuses_outside_429_and_5xx_do_not_count_as_an_outage(tmp_path, status):
    client, report, _ = run_many(tmp_path, 6, [Statused(status)] * 6)
    assert len(client.images.prompts) == 6 and report["stopped_early"] is None


@pytest.mark.parametrize("make", [
    lambda: ServerError("500"), lambda: Unavailable("503"), lambda: RateLimited("429"),
    lambda: ConnectionLost("timeout"), lambda: ProxyError(502), lambda: ProxyError(429),
    lambda: Statused(599)])
def test_every_transient_failure_kind_counts_toward_the_streak(tmp_path, make):
    client, report, _ = run_many(tmp_path, 5, [make(), make(), make(), png(), png()])
    assert len(client.images.prompts) == 9 and report["stopped_early"] == "consecutive_failures"
    assert statuses(report) == ["fallback"] * 5


def test_different_transient_kinds_add_up_to_one_streak(tmp_path):
    client, report, _ = run_many(tmp_path, 4, [ServerError("a"), ConnectionLost("b"), RateLimited("c"), png()])
    assert len(client.images.prompts) == 9 and report["stopped_early"] == "consecutive_failures"


def test_a_skipped_scene_neither_adds_to_nor_resets_the_streak(tmp_path):
    (tmp_path / "img_s2.png").write_bytes(png((9, 9, 9)))
    client, report, _ = run_many(tmp_path, 5, [ServerError("a"), ServerError("b"), ServerError("c"), png()])
    assert statuses(report) == ["fallback", "skipped", "fallback", "fallback", "fallback"]
    assert len(client.images.prompts) == 9 and report["stopped_early"] == "consecutive_failures"
    assert (report["generated"], report["fallback"], report["skipped"]) == (0, 4, 1)


def test_existing_images_after_the_trip_are_still_reported_as_skipped_and_reused(tmp_path):
    (tmp_path / "img_s5.png").write_bytes(png((9, 9, 9)))
    client, report, _ = run_many(tmp_path, 6, [ServerError("a")] * 3 + [png()] * 3)
    assert statuses(report) == ["fallback"] * 4 + ["skipped", "fallback"]
    assert (tmp_path / "img_s5.png").read_bytes() == png((9, 9, 9))
    assert (tmp_path / "img_s6.png").read_bytes() == png((9, 9, 9))
    assert len(client.images.prompts) == 9
    assert (report["generated"], report["fallback"], report["skipped"]) == (0, 5, 1)


# --- wall-clock cap ---------------------------------------------------------


def clocked_run(tmp_path, count, outcomes, cap, step, **kw):
    clock = FakeClock()
    client = ClockedClient(expand(outcomes), clock, step)
    _, report, throttles = run_many(tmp_path, count, outcomes, client=client, max_stage_sec=cap, clock=clock, **kw)
    return client, report, throttles


def test_the_stage_stops_calling_the_service_when_elapsed_equals_the_cap(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        client, report, throttles = clocked_run(tmp_path, 4, [png((1, 1, 1)), png((2, 2, 2))] + [png()] * 2,
                                                cap=1200, step=600)
    assert len(client.images.prompts) == 2
    assert statuses(report) == ["generated", "generated", "fallback", "fallback"]
    assert report["stopped_early"] == "time_limit" and (report["generated"], report["fallback"]) == (2, 2)
    assert (tmp_path / "img_s3.png").read_bytes() == png((2, 2, 2))
    assert (tmp_path / "img_s4.png").read_bytes() == png((2, 2, 2))
    assert len(throttles) == 1, "no throttle once the cap is reached"
    [record] = breaker_errors(caplog)
    assert "1200" in record.getMessage()


def test_the_stage_keeps_going_while_elapsed_is_below_the_cap(tmp_path):
    client, report, throttles = clocked_run(tmp_path, 4, [png()] * 4, cap=1201, step=600)
    assert len(client.images.prompts) == 3
    assert statuses(report) == ["generated"] * 3 + ["fallback"]
    assert report["stopped_early"] == "time_limit" and len(throttles) == 2


def test_a_run_that_finishes_inside_the_cap_reports_no_early_stop(tmp_path):
    _, report, _ = clocked_run(tmp_path, 3, [png()] * 3, cap=1801, step=600)
    assert report["stopped_early"] is None and report["generated"] == 3


def test_the_elapsed_time_is_measured_from_the_start_of_the_stage(tmp_path):
    clock = FakeClock(now=50_000.0)
    client = ClockedClient(expand([png()] * 3), clock, 10)
    run_many(tmp_path, 3, [], client=client, max_stage_sec=100, clock=clock)
    assert len(client.images.prompts) == 3


def test_an_existing_image_past_the_cap_is_still_skipped_and_reused(tmp_path):
    (tmp_path / "img_s2.png").write_bytes(png((9, 9, 9)))
    client, report, _ = clocked_run(tmp_path, 3, [png((1, 1, 1)), png()], cap=1200, step=1200)
    assert statuses(report) == ["generated", "skipped", "fallback"]
    assert report["stopped_early"] == "time_limit" and len(client.images.prompts) == 1
    assert (tmp_path / "img_s2.png").read_bytes() == png((9, 9, 9))
    assert (tmp_path / "img_s3.png").read_bytes() == png((9, 9, 9))


def test_the_time_limit_logs_one_error_even_with_many_scenes_left(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        clocked_run(tmp_path, 8, [png()] * 8, cap=1, step=5)
    assert len(breaker_errors(caplog)) == 1


def test_the_consecutive_failure_reason_wins_when_both_limits_are_reached(tmp_path):
    client, report, _ = clocked_run(tmp_path, 5, [ServerError("a")] * 3 + [png()] * 2, cap=13.5, step=1.5)
    assert len(client.images.prompts) == 9
    assert report["stopped_early"] == "consecutive_failures"


def test_the_defaults_are_one_hour_on_the_real_monotonic_clock(tmp_path):
    params = inspect.signature(gi.generate_scene_images).parameters
    assert params["max_stage_sec"].default == 4500 and params["clock"].default is time.monotonic
    client, report, _ = run_many(tmp_path, 3, [png()] * 3)
    assert len(client.images.prompts) == 3 and report["stopped_early"] is None


# --- main(): IMAGE_STAGE_MAX_SEC and re-runs -------------------------------


@pytest.fixture
def stage_caps(monkeypatch):
    seen = []
    original = gi.generate_scene_images

    def spy(*args, **kwargs):
        seen.append(kwargs.get("max_stage_sec"))
        return original(*args, **kwargs)

    monkeypatch.setattr(gi, "generate_scene_images", spy)
    return seen


@pytest.mark.parametrize("configured,expected", [
    (None, 4500), ("", 4500), ("900", 900), ("90.5", 90.5), (" 120 ", 120),
    ("junk", 4500), ("0", 4500), ("-5", 4500), ("nan", 4500), ("inf", 4500), ("1e999", 4500)])
def test_main_reads_the_stage_cap_from_the_environment(monkeypatch, episode_env, sleeps, stage_caps,
                                                         configured, expected):
    ep_dir = common.get_episode_dir(20)
    write_shots(ep_dir)
    monkeypatch.setenv("VIDEO_MODE", "shots")
    if configured is not None:
        monkeypatch.setenv("IMAGE_STAGE_MAX_SEC", configured)
    patch_client(monkeypatch, RecordingImageClient([png(), png(), png()]))
    assert gi.main(20) == 0
    assert stage_caps == [expected]


def test_main_writes_stopped_early_into_the_image_report(monkeypatch, episode_env, sleeps):
    ep_dir = common.get_episode_dir(21)
    write_shots(ep_dir, scenes=many_shots(5)["scenes"])
    monkeypatch.setenv("VIDEO_MODE", "shots")
    pin_jitter_to_upper_bound(monkeypatch)
    client = RecordingImageClient(expand([png((1, 1, 1)), ServerError("a"), ServerError("b"), ServerError("c"), png()]))
    patch_client(monkeypatch, client)
    assert gi.main(21) == 0
    report = read_report(ep_dir)
    assert report["stopped_early"] == "consecutive_failures"
    assert [s["status"] for s in report["scenes"]] == ["generated"] + ["fallback"] * 4
    assert (report["generated"], report["fallback"], report["skipped"]) == (1, 4, 0)
    assert len(client.images.kwargs) == 1 + 9
    assert sleeps.count(30 + 10) == 3, "throttle after the first three scenes only"


def test_a_tripped_run_is_retried_scene_by_scene_on_the_next_run(monkeypatch, episode_env, sleeps, caplog):
    ep_dir = common.get_episode_dir(22)
    write_shots(ep_dir, scenes=many_shots(5)["scenes"])
    monkeypatch.setenv("VIDEO_MODE", "shots")
    first = png((1, 1, 1))
    patch_client(monkeypatch, RecordingImageClient(
        expand([first, ServerError("a"), ServerError("b"), ServerError("c"), png()])))
    gi.main(22)
    assert read_report(ep_dir)["stopped_early"] == "consecutive_failures"
    assert all((ep_dir / f"img_s{k}.png").exists() for k in range(1, 6))

    retry_client = RecordingImageClient([png((k, k, k)) for k in range(2, 6)])
    patch_client(monkeypatch, retry_client)
    with caplog.at_level(logging.INFO):
        assert gi.main(22) == 0
    report = read_report(ep_dir)
    assert [s["status"] for s in report["scenes"]] == ["skipped"] + ["generated"] * 4
    assert (report["generated"], report["fallback"], report["skipped"]) == (4, 0, 1)
    assert report["stopped_early"] is None
    assert (ep_dir / "img_s1.png").read_bytes() == first
    assert (ep_dir / "img_s5.png").read_bytes() == png((5, 5, 5))
    assert len(retry_client.images.kwargs) == 4
    assert any("有 4 個" in record.getMessage() for record in caplog.records)


def test_the_slideshow_path_has_no_circuit_breaker(monkeypatch, episode_env, sleeps):
    ep_dir = common.get_episode_dir(23)
    write_script(ep_dir, sections=5)
    client = RecordingImageClient([ServerError("down")] * 15)
    patch_client(monkeypatch, client)
    monkeypatch.setattr(gi, "_write_fallback", lambda path: path.write_bytes(png()))
    assert gi.main(23) == 0
    assert len(client.images.kwargs) == 15, "every section still tries the service, as before"
    assert all((ep_dir / f"img_{k:02d}.png").exists() for k in range(1, 6))
    assert not (ep_dir / "image_report.json").exists()
