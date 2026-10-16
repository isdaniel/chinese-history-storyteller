"""YouTube 上傳:status.containsSyntheticMedia 固定宣告 false (不論畫面風格)。"""
import json
import logging

import pytest

import common
import upload_youtube as uu


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("VIDEO_MODE", "VISUAL_STYLE", "YOUTUBE_PRIVACY", "GITHUB_ENV"):
        monkeypatch.delenv(key, raising=False)


SCRIPT = {"title": "秦始皇陵之謎", "description": "摘要", "tags": ["秦始皇陵", "兵馬俑"]}
TIMINGS = {"sections": [{"start": 0.0, "narration": "開場。"}, {"start": 75.0, "narration": "第二段。"}]}


# --- the request body ------------------------------------------------------------------------------------------------

def test_the_body_declares_no_synthetic_media():
    body = uu.build_body(SCRIPT, TIMINGS, "public")
    assert body == {
        "snippet": {"title": "秦始皇陵之謎", "description": uu.build_description(SCRIPT, TIMINGS),
                    "tags": ["秦始皇陵", "兵馬俑"], "categoryId": "22", "defaultLanguage": "zh-Hant",
                    "defaultAudioLanguage": "zh-Hant"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "license": "youtube",
                   "embeddable": True, "containsSyntheticMedia": False}}


def test_the_flag_is_sent_explicitly_as_a_json_false_not_omitted():
    status = uu.build_body(SCRIPT, TIMINGS, "private")["status"]
    assert "containsSyntheticMedia" in status and status["containsSyntheticMedia"] is False
    assert json.loads(json.dumps(status))["containsSyntheticMedia"] is False
    assert uu.CONTAINS_SYNTHETIC_MEDIA is False


@pytest.mark.parametrize("privacy", ["public", "unlisted", "private"])
def test_the_privacy_setting_is_passed_through(privacy):
    assert uu.build_body(SCRIPT, TIMINGS, privacy)["status"]["privacyStatus"] == privacy


def test_the_style_logic_is_gone():
    assert not hasattr(uu, "declares_synthetic_media") and not hasattr(uu, "video_style")


# --- main(): what is really sent to YouTube --------------------------------------------------------------------------

class FakeYouTube:
    def __init__(self, sent):
        self.sent = sent

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.sent.update(part=part, body=body)
        return self

    def next_chunk(self):
        return None, {"id": "VID123"}


@pytest.fixture
def upload(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github_env"))
    sent = {}
    monkeypatch.setattr(uu, "get_credentials", lambda: None)
    monkeypatch.setattr(uu, "build", lambda *args, **kwargs: FakeYouTube(sent))
    ep = common.get_episode_dir(9)
    (ep / "script.json").write_text(json.dumps(SCRIPT, ensure_ascii=False), encoding="utf-8")
    (ep / "timings.json").write_text(json.dumps(TIMINGS, ensure_ascii=False), encoding="utf-8")
    (ep / "final.mp4").write_bytes(b"not really a video")
    return ep, sent


@pytest.mark.parametrize("style", [None, "cinematic", "ink", "neon"])
@pytest.mark.parametrize("mode", [None, "slideshow", "shots"])
def test_every_upload_declares_false_whatever_the_style_or_mode(monkeypatch, upload, style, mode):
    ep, sent = upload
    if style:
        monkeypatch.setenv("VISUAL_STYLE", style)
    if mode:
        monkeypatch.setenv("VIDEO_MODE", mode)
    assert uu.main(9) == 0
    assert sent["part"] == "snippet,status"
    assert sent["body"]["status"]["containsSyntheticMedia"] is False


def test_privacy_and_the_result_files_are_unchanged(monkeypatch, upload, tmp_path):
    ep, sent = upload
    monkeypatch.setenv("YOUTUBE_PRIVACY", "private")
    assert uu.main(9) == 0
    assert sent["body"]["status"]["privacyStatus"] == "private"
    assert json.loads((ep / "youtube_info.json").read_text(encoding="utf-8")) == {
        "video_id": "VID123", "url": "https://youtu.be/VID123"}
    assert "YOUTUBE_VIDEO_ID=VID123" in (tmp_path / "github_env").read_text(encoding="utf-8")


def test_the_default_privacy_is_public(upload):
    ep, sent = upload
    uu.main(9)
    assert sent["body"]["status"]["privacyStatus"] == "public"


def test_the_log_says_what_was_declared(upload, caplog):
    ep, sent = upload
    with caplog.at_level(logging.INFO, logger="upload_youtube"):
        uu.main(9)
    assert any("containsSyntheticMedia=false" in r.getMessage() for r in caplog.records)
