"""Azure OpenAI 共用小工具:建立 client、呼叫 gpt-5-mini 取回 JSON (可附一張圖片)。"""
import json

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI, OpenAI

from common import env

SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_GPT = "gpt-5-mini"


def make_client(timeout: float = 300.0):
    """有設 AZURE_OPENAI_API_VERSION 就走既有的 AzureOpenAI 路徑 (行為與舊腳本相同);
    沒設 (例如本機) 就走 v1 路徑,不需要指定 api-version。"""
    token_provider = get_bearer_token_provider(DefaultAzureCredential(), SCOPE)
    endpoint = env("AZURE_OPENAI_ENDPOINT")
    api_version = env("AZURE_OPENAI_API_VERSION", required=False)
    if api_version:
        return AzureOpenAI(azure_ad_token_provider=token_provider, api_version=api_version,
                           azure_endpoint=endpoint, max_retries=5, timeout=timeout)
    return OpenAI(base_url=endpoint.rstrip("/") + "/openai/v1/", api_key=token_provider,
                  max_retries=5, timeout=timeout)


def chat_json(client, prompt: str, *, image_b64: str | None = None, deployment: str | None = None,
              system: str = "輸出嚴格 JSON,不要任何其他文字。") -> dict:
    content = prompt if image_b64 is None else [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
    ]
    response = client.chat.completions.create(
        model=deployment or env("AZURE_OPENAI_GPT_DEPLOYMENT", DEFAULT_GPT),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
        response_format={"type": "json_object"},
        max_completion_tokens=20000,
        reasoning_effort="low",
    )
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError(f"empty reply: finish_reason={response.choices[0].finish_reason}")
    return json.loads(text)
