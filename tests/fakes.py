import base64
import json
from types import SimpleNamespace


class FakeImages:
    """images.generate returns queued PNG bytes (or raises queued exceptions)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []

    def generate(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(outcome).decode())])


class BadRequest(Exception):
    status_code = 400


class FakeImageClient:
    def __init__(self, outcomes):
        self.images = FakeImages(outcomes)


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) or reply is None else json.dumps(reply, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
                               usage=None)


class FakeClient:
    """Stands in for AzureOpenAI/OpenAI: chat.completions.create returns queued replies."""

    def __init__(self, replies=()):
        self.chat = SimpleNamespace(completions=FakeChat(replies))

    @property
    def calls(self):
        return self.chat.completions.calls

    def prompts(self):
        out = []
        for call in self.calls:
            content = call["messages"][-1]["content"]
            out.append(content if isinstance(content, str) else content[0]["text"])
        return out
