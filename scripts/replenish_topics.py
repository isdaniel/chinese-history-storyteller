"""補充 topics_queue.json:用 Azure OpenAI 產生新題目並 append 進 queue。

預設不會自動 commit/PR — 產生後請人工檢查 diff 再決定是否提交。

用法:
    python scripts/replenish_topics.py                # 預設產 30 筆
    python scripts/replenish_topics.py --count 50
    python scripts/replenish_topics.py --dry-run      # 印到 stdout 不寫檔
"""
import argparse
import json
import random
import sys
from collections import Counter

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from common import DATA_DIR, TEMPLATES_DIR, env, load_json, save_json, setup_logging

log = setup_logging("replenish_topics")

VALID_CATEGORIES = {"chinese_history", "tech_history", "world_civilization", "mystery"}


def build_prompt(queue: dict, log_data: dict, n: int, next_id: int) -> str:
    template = (TEMPLATES_DIR / "replenish_prompt.txt").read_text(encoding="utf-8")
    used_titles = "\n".join(f"- #{e['id']} {e['title']}" for e in log_data["published"])
    published_ids = {e["id"] for e in log_data["published"]}
    pending = [t for t in queue["topics"] if t["id"] not in published_ids]
    pending_titles = "\n".join(f"- #{t['id']} {t['title']}" for t in pending)
    # 取 8 筆既有題目當風格範例 (各類別均勻抽)
    by_cat = {c: [t for t in queue["topics"] if t["category"] == c] for c in VALID_CATEGORIES}
    examples_list = []
    for c, items in by_cat.items():
        examples_list.extend(random.sample(items, min(2, len(items))))
    examples = json.dumps(examples_list, ensure_ascii=False, indent=2)
    return template.format(
        used_titles=used_titles or "(尚無)",
        pending_titles=pending_titles or "(尚無)",
        n=n,
        next_id=next_id,
        min_per_category=max(1, n // 5),
        examples=examples,
    )


def call_llm(prompt: str, n: int) -> list:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_ad_token_provider=token_provider,
        api_version=env("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        azure_endpoint=env("AZURE_OPENAI_ENDPOINT"),
        max_retries=5,
        timeout=300.0,
    )
    deployment = env("AZURE_OPENAI_GPT_DEPLOYMENT", "gpt-5-mini")
    log.info("呼叫 %s 產 %d 筆題目…", deployment, n)
    max_tokens = max(8000, n * 250 + 4000)
    resp = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": "你是中文 podcast 選題編輯。輸出嚴格 JSON。"},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=max_tokens,
        reasoning_effort="medium",
    )
    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError(f"LLM 回傳空內容: finish={resp.choices[0].finish_reason} usage={resp.usage}")
    data = json.loads(content)
    return data.get("topics", [])


def validate(topics: list, existing_ids: set, existing_titles: set) -> list:
    """驗證並過濾新題目:schema、id 不重、title 不重、category 合法。"""
    valid = []
    for t in topics:
        try:
            tid = int(t["id"])
            title = str(t["title"]).strip()
            cat = t["category"]
            tags = t.get("tags", [])
            kp = t.get("key_points", [])
            length_min = int(t.get("length_min", 9))
        except (KeyError, ValueError, TypeError) as e:
            log.warning("跳過格式錯誤題目: %s (%s)", t, e)
            continue
        if cat not in VALID_CATEGORIES:
            log.warning("跳過 category 非法: #%d %s (%s)", tid, title, cat)
            continue
        if tid in existing_ids:
            log.warning("跳過 id 重複: #%d %s", tid, title)
            continue
        if title in existing_titles:
            log.warning("跳過 title 重複: #%d %s", tid, title)
            continue
        if not (3 <= len(kp) <= 8) or not (2 <= len(tags) <= 6):
            log.warning("跳過 tags/key_points 數量不對: #%d %s", tid, title)
            continue
        existing_ids.add(tid)
        existing_titles.add(title)
        valid.append({
            "id": tid, "title": title, "category": cat,
            "tags": tags, "length_min": length_min, "key_points": kp,
        })
    return valid


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=30, help="產生題目數 (預設 30)")
    p.add_argument("--dry-run", action="store_true", help="只印 JSON 不寫入 queue")
    args = p.parse_args()

    queue = load_json(DATA_DIR / "topics_queue.json")
    log_data = load_json(DATA_DIR / "published_log.json")

    existing_ids = {t["id"] for t in queue["topics"]} | {e["id"] for e in log_data["published"]}
    existing_titles = {t["title"] for t in queue["topics"]} | {e["title"] for e in log_data["published"]}
    next_id = max(existing_ids) + 1 if existing_ids else 1

    prompt = build_prompt(queue, log_data, args.count, next_id)
    raw_topics = call_llm(prompt, args.count)
    log.info("LLM 回傳 %d 筆,開始驗證…", len(raw_topics))

    new_topics = validate(raw_topics, existing_ids, existing_titles)
    cat_counts = Counter(t["category"] for t in new_topics)
    log.info("通過驗證 %d 筆,類別分布: %s", len(new_topics), dict(cat_counts))

    if not new_topics:
        log.error("沒有任何題目通過驗證")
        return 1

    if args.dry_run:
        print(json.dumps({"topics": new_topics}, ensure_ascii=False, indent=2))
        return 0

    queue["topics"].extend(new_topics)
    save_json(DATA_DIR / "topics_queue.json", queue)
    log.info(
        "已 append %d 筆進 topics_queue.json (id %d~%d)。請 git diff 檢查後 commit。",
        len(new_topics), new_topics[0]["id"], new_topics[-1]["id"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
