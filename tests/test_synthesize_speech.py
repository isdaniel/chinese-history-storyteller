from datetime import timedelta
from types import SimpleNamespace

import pytest

import timing_units as tu


def test_get_audio_duration_reads_mp3_length(tmp_path, ffmpeg_exe):
    import subprocess
    import synthesize_speech as ss

    mp3 = tmp_path / "tone.mp3"
    subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)], check=True)
    assert ss.get_audio_duration(mp3) == pytest.approx(2.0, abs=0.1)


def test_concat_mp3_joins_parts(tmp_path, ffmpeg_exe):
    import subprocess
    import synthesize_speech as ss

    parts = []
    for k in range(2):
        mp3 = tmp_path / f"p{k}.mp3"
        subprocess.run([ffmpeg_exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3)], check=True)
        parts.append(mp3)
    out = tmp_path / "all.mp3"
    ss.concat_mp3(parts, out)
    assert ss.get_audio_duration(out) == pytest.approx(2.0, abs=0.15)


def test_ssml_escapes_markup_and_uses_the_voice(monkeypatch):
    import synthesize_speech as ss

    monkeypatch.delenv("TTS_RATE", raising=False)
    ssml = ss.make_ssml("A & B <c>", "zh-CN-YunjianNeural")
    assert "A &amp; B &lt;c&gt;" in ssml and "zh-CN-YunjianNeural" in ssml and "rate='-8%'" in ssml


def test_upload_chapters_still_work_with_the_new_timings_shape():
    import upload_youtube

    S0, S1 = "甲乙丙丁戊己庚辛壬，子丑寅卯辰巳午未申。", "天地玄黃宇宙洪荒日月盈昃。"
    events = [{"type": "X.Sentence", "offset": 0.05, "dur": 4.5, "text": S0},
              {"type": "X.Sentence", "offset": 4.6, "dur": 2.8, "text": S1}]
    timings = tu.build_timings([{"section_id": 1, "narration": S0 + S1, "audio_len": 7.5, "events": events},
                                {"section_id": 2, "narration": S1, "audio_len": 7.5, "events": []}])
    text = upload_youtube.build_description({"description": "摘要"}, timings)
    assert "00:00 甲乙丙丁戊己庚辛壬，子丑寅卯辰巳午未申" in text
    assert "00:07 天地玄黃宇宙洪荒日月盈昃" in text


def test_synthesize_section_requests_sentence_boundaries_and_keeps_only_the_final_attempts_events(
        tmp_path, monkeypatch):
    import synthesize_speech as ss

    sdk = ss.speechsdk
    monkeypatch.setattr("retry.time.sleep", lambda _seconds: None)

    def boundary(kind, offset, seconds, text):
        return SimpleNamespace(boundary_type=f"SpeechSynthesisBoundaryType.{kind}", audio_offset=offset,
                               duration=timedelta(seconds=seconds), text=text)

    failed = SimpleNamespace(
        reason=sdk.ResultReason.Canceled,
        cancellation_details=SimpleNamespace(reason="Error", error_code="ConnectionFailure", error_details="boom"))
    completed = SimpleNamespace(reason=sdk.ResultReason.SynthesizingAudioCompleted)
    attempts = [
        (failed, [boundary("Sentence", 0, 1.0, "STALE")]),
        (completed, [boundary("Sentence", 500_000, 4.5, "甲乙丙。"),
                     boundary("Word", 500_000, 0.25, "甲乙"),
                     boundary("Punctuation", 45_000_000, 0.125, "。")]),
    ]
    created, ssml_sent, audio_files = [], [], []

    class FakeSignal:
        def __init__(self):
            self.handlers = []

        def connect(self, handler):
            self.handlers.append(handler)

    class FakeSynth:
        def __init__(self, speech_config, audio_config):
            self.synthesis_word_boundary = FakeSignal()
            self.attempt = len(created)
            created.append(self)
            audio_files.append(audio_config.filename)

        def speak_ssml_async(self, ssml):
            ssml_sent.append(ssml)
            result, events = attempts[self.attempt]
            for evt in events:
                for handler in self.synthesis_word_boundary.handlers:
                    handler(evt)
            return SimpleNamespace(get=lambda: result)

    monkeypatch.setattr(sdk, "SpeechSynthesizer", FakeSynth)
    monkeypatch.setattr(sdk.audio, "AudioOutputConfig", lambda filename: SimpleNamespace(filename=filename))

    cfg_calls = []
    cfg = SimpleNamespace(
        set_speech_synthesis_output_format=lambda fmt: cfg_calls.append(("format", fmt)),
        set_property=lambda prop, value: cfg_calls.append(("property", prop, value)))

    cleaned = tu.tts_text("「甲乙」丙。 丁！")
    out_path = tmp_path / "audio_01.mp3"
    events = ss.synthesize_section(cleaned, out_path, "zh-CN-YunjianNeural", cfg)

    assert ("property", sdk.PropertyId.SpeechServiceResponse_RequestSentenceBoundary, "true") in cfg_calls
    assert ("format", sdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3) in cfg_calls
    assert events == [
        {"type": "SpeechSynthesisBoundaryType.Sentence", "offset": 0.05, "dur": 4.5, "text": "甲乙丙。"},
        {"type": "SpeechSynthesisBoundaryType.Word", "offset": 0.05, "dur": 0.25, "text": "甲乙"},
        {"type": "SpeechSynthesisBoundaryType.Punctuation", "offset": 4.5, "dur": 0.125, "text": "。"},
    ]
    assert len(created) == 2 and audio_files == [str(out_path)] * 2
    assert len(ssml_sent) == 2
    for ssml in ssml_sent:
        assert cleaned in ssml and "zh-CN-YunjianNeural" in ssml and "「" not in ssml
