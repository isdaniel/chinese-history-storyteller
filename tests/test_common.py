import json
import sys
import types

import pytest

import common


def test_ffmpeg_bin_prefers_env(monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", "/opt/custom/ffmpeg")
    assert common.ffmpeg_bin() == "/opt/custom/ffmpeg"


def test_ffmpeg_bin_uses_path_then_imageio(monkeypatch):
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setattr(common.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert common.ffmpeg_bin() == "/usr/bin/ffmpeg"
    monkeypatch.setattr(common.shutil, "which", lambda name: None)
    fake = types.SimpleNamespace(get_ffmpeg_exe=lambda: "/venv/imageio/ffmpeg")
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)
    assert common.ffmpeg_bin() == "/venv/imageio/ffmpeg"


def _queue(tmp_path, published_ids):
    topics = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}, {"id": 3, "title": "c"}]
    (tmp_path / "topics_queue.json").write_text(json.dumps({"topics": topics}), encoding="utf-8")
    log = {"published": [{"id": i} for i in published_ids]}
    (tmp_path / "published_log.json").write_text(json.dumps(log), encoding="utf-8")


def test_pick_next_topic_skips_published(monkeypatch, tmp_path):
    _queue(tmp_path, [1])
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setattr(common, "notify_discord", lambda message: None)
    monkeypatch.delenv("TOPIC_ID", raising=False)
    assert common.pick_next_topic()["id"] == 2


def test_topic_id_override_ignores_published(monkeypatch, tmp_path):
    _queue(tmp_path, [1])
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TOPIC_ID", "1")
    assert common.pick_next_topic()["id"] == 1


def test_topic_id_override_unknown_raises(monkeypatch, tmp_path):
    _queue(tmp_path, [])
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)
    monkeypatch.setenv("TOPIC_ID", "99")
    with pytest.raises(RuntimeError):
        common.pick_next_topic()
