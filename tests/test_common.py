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


def test_save_json_replaces_the_file_atomically(tmp_path):
    path = tmp_path / "data.json"
    common.save_json(path, {"a": "中文"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "中文"}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"], "no temporary file is left behind"
    common.save_json(path, {"a": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}


def test_a_failed_save_keeps_the_previous_file_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "data.json"
    common.save_json(path, {"keep": "me"})
    with pytest.raises(TypeError):
        common.save_json(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": "me"}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_an_interrupted_save_is_cleaned_up_too(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    monkeypatch.setattr(common.json, "dump", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        common.save_json(path, {"a": 1})
    assert list(tmp_path.iterdir()) == []
