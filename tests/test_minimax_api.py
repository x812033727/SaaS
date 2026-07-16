from types import SimpleNamespace

from saas_mvp.ai import minimax_api


class _Completions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="直接回覆", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_text_query_calls_minimax_m3_directly(monkeypatch):
    completions = _Completions()
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(minimax_api, "_client", factory)
    result = minimax_api.text_query(
        prompt="你好",
        system_prompt="請用繁中",
        api_key="secret-key",
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M3",
    )

    assert result == "直接回覆"
    assert captured == {
        "api_key": "secret-key",
        "base_url": "https://api.minimax.io/v1",
    }
    call = completions.calls[0]
    assert call["model"] == "MiniMax-M3"
    assert call["extra_body"]["thinking"] == {"type": "disabled"}
    assert call["messages"][1] == {"role": "user", "content": "你好"}
