"""Step 3: Azure Speech Service 中文 TTS 合成,每段一個 mp3 並紀錄時長以做字幕。

輸入: output/ep{id}/script.json
輸出: output/ep{id}/audio_01.mp3 ... audio_08.mp3
       output/ep{id}/audio_full.mp3
       output/ep{id}/timings.json (每段起訖秒數,做字幕用)
"""
import os
import subprocess
import sys
from pathlib import Path

import requests

import azure.cognitiveservices.speech as speechsdk
from azure.identity import DefaultAzureCredential

from common import env, get_episode_dir, load_json, save_json, setup_logging
from retry import with_backoff

log = setup_logging("synthesize_speech")

FFMPEG  = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


def make_ssml(text: str, voice: str) -> str:
    safe = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
    # rate 預設 -8% (ep1~ep5 一致),可由 TTS_RATE env 覆寫做 A/B 測試。
    # ep5 實測: -8% 下 zh-CN-YunjianNeural 約 291 字/分。
    # 若改 -15% 預估降至 ~268 字/分,但需要實測驗證。
    rate = os.environ.get("TTS_RATE", "-8%")
    return f"""<speak version='1.0' xml:lang='zh-CN' xmlns:mstts='https://www.w3.org/2001/mstts'>
<voice name='{voice}'>
<mstts:express-as style='narration-professional'>
<prosody rate='{rate}' pitch='-2%'>{safe}</prosody>
</mstts:express-as>
</voice></speak>"""


def synthesize_section(text: str, out_path: Path, voice: str, speech_cfg) -> float:
    speech_cfg.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Audio48Khz192KBitRateMonoMp3
    )

    def _do() -> float:
        audio_cfg = speechsdk.audio.AudioOutputConfig(filename=str(out_path))
        synth = speechsdk.SpeechSynthesizer(speech_config=speech_cfg, audio_config=audio_cfg)
        result = synth.speak_ssml_async(make_ssml(text, voice)).get()

        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            details = ""
            if result.reason == speechsdk.ResultReason.Canceled:
                cd = result.cancellation_details
                details = f"reason={cd.reason} code={cd.error_code} details={cd.error_details}"
            # 統一拋 RuntimeError;retry helper 無 status 就視為可重試
            raise RuntimeError(f"TTS 失敗: {result.reason} {details}")
        return result.audio_duration.total_seconds() if result.audio_duration else 0.0

    # Speech SDK 沒有內建 retry;F0 tier 偶爾 throttle 或 transient 失敗時自己退避
    return with_backoff(_do, max_attempts=4, base_sec=3.0, max_sec=30.0,
                        jitter_sec=2.0, op_name=f"TTS {out_path.name}")


def concat_mp3(parts: list, out_path: Path) -> None:
    list_file = out_path.parent / "concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8"
    )
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out_path)],
        check=True, capture_output=True,
    )
    list_file.unlink(missing_ok=True)


def get_audio_duration(path: Path) -> float:
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.strip())


def main(episode_id: int) -> int:
    ep_dir = get_episode_dir(episode_id)
    script = load_json(ep_dir / "script.json")

    voice = env("AZURE_SPEECH_VOICE", "zh-CN-YunjianNeural")
    region = env("AZURE_SPEECH_REGION", "eastus")
    custom_domain = env("AZURE_SPEECH_CUSTOM_DOMAIN")
    cred = DefaultAzureCredential()
    aad_token = cred.get_token("https://cognitiveservices.azure.com/.default").token
    # Exchange AAD token for short-lived Speech authToken via custom-subdomain endpoint.
    # SDK auth_token expects the short-lived token directly when paired with region.
    issue_url = f"https://{custom_domain}.cognitiveservices.azure.com/sts/v1.0/issueToken"

    def _issue_token() -> str:
        r = requests.post(issue_url, headers={"Authorization": f"Bearer {aad_token}"}, timeout=10)
        r.raise_for_status()
        return r.text

    auth_token = with_backoff(_issue_token, max_attempts=4, base_sec=2.0, max_sec=20.0,
                              jitter_sec=2.0, op_name="Speech STS issueToken")
    speech_cfg = speechsdk.SpeechConfig(auth_token=auth_token, region=region)

    timings = []
    parts = []
    cumulative = 0.0
    for section in script["sections"]:
        idx = section["section_id"]
        out_path = ep_dir / f"audio_{idx:02d}.mp3"
        log.info("[%d] 合成語音…", idx)
        synthesize_section(section["narration"], out_path, voice, speech_cfg)
        dur = get_audio_duration(out_path)
        timings.append({
            "section_id": idx,
            "start": cumulative,
            "end": cumulative + dur,
            "duration": dur,
            "narration": section["narration"],
        })
        cumulative += dur
        parts.append(out_path)
        log.info("[%d] 時長 %.1fs (累計 %.1fs)", idx, dur, cumulative)

    full_path = ep_dir / "audio_full.mp3"
    concat_mp3(parts, full_path)
    log.info("已合併完整音檔: %s (總長 %.1fs)", full_path.name, cumulative)

    save_json(ep_dir / "timings.json", {"sections": timings, "total": cumulative})
    return 0


if __name__ == "__main__":
    eid = int(os.environ.get("EPISODE_ID") or sys.argv[1])
    sys.exit(main(eid))
