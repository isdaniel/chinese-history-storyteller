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
    # 校準歷史:
    #   v1 (前 4 集): 220 字/分 → LLM 給字不足,實際 4~7 分鐘
    #   v2 (ep5):    145 字/分 + prompt 強制下限 → LLM 給滿 1540 字,實測 5:17
    #                從 ep5 CI log 反推真實語速 = 1540/317.3*60 = 291 字/分
    #   v3 (本次):   chars_per_min=291 (實測值),buffer 拉到 1.15 對抗 LLM 略低於目標的傾向
    chars_per_min = int(env("CHARS_PER_MIN", "291"))
    sections = int(env("SECTIONS_PER_EPISODE", "8"))
    target_chars = int(length_min * chars_per_min * 1.15)
    min_chars = int(length_min * chars_per_min * 1.00)
    return template.format(
        title=topic["title"],
        category=topic["category"],
        key_points="、".join(topic.get("key_points", [])),
        length_min=length_min,
        target_chars=target_chars,
        min_chars=min_chars,
        sections=sections,
        last_section=sections,
        chars_per_section=target_chars // sections,
        min_chars_per_section=min_chars // sections,
    )


def generate(topic: dict) -> dict:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    # max_retries=5:SDK 內建會 honor 429 的 Retry-After header 做指數退避
    # timeout=300:gpt-5-mini reasoning 可能跑很久,給寬裕上限
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
        max_retries=5,
        timeout=300.0,
    )
    deployment = env("AZURE_OPENAI_GPT_DEPLOYMENT", "gpt-5-mini")

    log.info("呼叫 Azure OpenAI (deployment=%s) 生成腳本…", deployment)
    # gpt-5-mini 是 reasoning model:max_completion_tokens 同時涵蓋 reasoning tokens 與輸出
    # 校準後 9 分鐘目標 ~3000 字 ≈ 4.5K tokens 輸出,留 ~15K reasoning 餘裕
    target_tokens = max(20000, int(resolve_length_min(topic) * 2400))
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

    if "sections" not in script or len(script["sections"]) < max(4, int(env("SECTIONS_PER_EPISODE", "8")) - 2):
        raise RuntimeError(f"腳本格式錯誤,sections 不足: {script.keys()}")

    # 字數檢查:旁白總字數應接近目標,否則影片會明顯短於 length_min
    length_min = resolve_length_min(topic)
    chars_per_min = int(env("CHARS_PER_MIN", "291"))
    expected_min_chars = int(length_min * chars_per_min * 0.95)
    total_chars = sum(len(s.get("narration", "")) for s in script["sections"])
    log.info("旁白總字數 %d (目標下限 %d, %d 分鐘 @ %d 字/分)",
             total_chars, expected_min_chars, length_min, chars_per_min)
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
