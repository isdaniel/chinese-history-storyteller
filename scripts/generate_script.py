"""Step 1: 從題材庫取下一集 + 用 Azure OpenAI GPT-4o-mini 生成完整腳本。

輸出:
  output/ep{id}/script.json  (含 title, description, tags, sections[], closing_reflection)
"""
import json
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from common import (
    DATA_DIR, TEMPLATES_DIR, env, get_episode_dir, load_json,
    pick_next_topic, save_json, setup_logging,
)

log = setup_logging("generate_script")


def resolve_length_min(topic: dict) -> int:
    """Clamp queue 內 length_min 至目標 8~10 分鐘區間 (TARGET_LENGTH_MIN 可覆寫上限)。"""
    raw_length_min = topic.get("length_min", 12)
    target_min = int(env("TARGET_LENGTH_MIN", "9"))
    return max(8, min(10, min(raw_length_min, target_min)))


def build_prompt(topic: dict) -> str:
    template = (TEMPLATES_DIR / "script_prompt.txt").read_text(encoding="utf-8")
    # 目標影片長度 8~10 分鐘:把 queue 設定的 length_min clamp 到此區間
    # (queue 內既有資料多為 11~15 分,直接用會超過上限)
    length_min = resolve_length_min(topic)
    # 校準: zh-CN-YunjianNeural + narration-professional + rate=-8% 實測約 145 字/分
    # (前四集設 220 字/分,實測長度只有目標的 40~50%)
    # 為避免 LLM 又縮水,再加 15% buffer 拉高目標下限
    chars_per_min = 145
    target_chars = int(length_min * chars_per_min * 1.15)
    min_chars = int(length_min * chars_per_min * 0.95)
    return template.format(
        title=topic["title"],
        category=topic["category"],
        key_points="、".join(topic.get("key_points", [])),
        length_min=length_min,
        target_chars=target_chars,
        min_chars=min_chars,
        chars_per_section=target_chars // 8,
        min_chars_per_section=min_chars // 8,
    )


def generate(topic: dict) -> dict:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
    )
    deployment = env("AZURE_OPENAI_GPT_DEPLOYMENT", "gpt-5-mini")

    log.info("呼叫 Azure OpenAI (deployment=%s) 生成腳本…", deployment)
    # gpt-5-mini 是 reasoning model:max_completion_tokens 同時涵蓋 reasoning tokens 與輸出
    # 校準後 10 分鐘約 1700 字 ≈ 2.5K tokens 輸出,留 ~13K reasoning 餘裕
    target_tokens = max(16000, int(resolve_length_min(topic) * 1600))
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": "你是專業中文歷史說書人。輸出嚴格 JSON。"},
            {"role": "user", "content": build_prompt(topic)},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=target_tokens,
        reasoning_effort="low",
    )
    content = resp.choices[0].message.content
    if not content:
        finish = resp.choices[0].finish_reason
        usage = resp.usage
        raise RuntimeError(
            f"GPT 回傳空內容: finish_reason={finish}, usage={usage}. "
            "可能是 max_completion_tokens 不足,或 reasoning 吃掉太多 token。"
        )
    script = json.loads(content)

    if "sections" not in script or len(script["sections"]) < 6:
        raise RuntimeError(f"腳本格式錯誤,sections 不足: {script.keys()}")

    # 字數檢查:旁白總字數應接近目標,否則影片會明顯短於 length_min
    length_min = resolve_length_min(topic)
    expected_min_chars = int(length_min * 145 * 0.85)
    total_chars = sum(len(s.get("narration", "")) for s in script["sections"])
    log.info("旁白總字數 %d (目標下限 %d, %d 分鐘)", total_chars, expected_min_chars, length_min)
    if total_chars < expected_min_chars:
        log.warning(
            "旁白字數 %d 低於下限 %d,預計影片長度將短於 %d 分鐘",
            total_chars, expected_min_chars, length_min,
        )

    log.info("生成完成:%s (%d 段)", script.get("title"), len(script["sections"]))
    return script


def main() -> int:
    topic = pick_next_topic()
    log.info("選定題材 #%d: %s", topic["id"], topic["title"])

    script = generate(topic)
    script["topic_id"] = topic["id"]
    script["original_topic"] = topic

    ep_dir = get_episode_dir(topic["id"])
    save_json(ep_dir / "script.json", script)
    log.info("已寫入 %s", ep_dir / "script.json")

    # 把 episode_id 寫進 GitHub Actions 的環境檔
    gh_env = Path(__import__("os").environ.get("GITHUB_ENV", "/dev/null"))
    if gh_env.parent.exists():
        try:
            with open(gh_env, "a", encoding="utf-8") as f:
                f.write(f"EPISODE_ID={topic['id']}\n")
        except Exception:
            pass
    print(f"EPISODE_ID={topic['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
