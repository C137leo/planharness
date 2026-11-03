import httpx

from app.domain.patches import create_knee_patch
from app.llm.deepseek import DeepSeekExplainer


def test_explain_without_key_uses_fallback():
    explainer = DeepSeekExplainer(
        api_key="",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        timeout_s=1,
    )
    result = explainer.explain(
        messages=[{"role": "system", "content": "你是教练"}, {"role": "user", "content": "膝痛"}],
        operations=[
            {
                "op": "replace_action",
                "before_action_ref": "goblet_squat",
                "after_action": {"action_ref": "glute_bridge"},
            }
        ],
        diff={"changed_slots": 3, "total_slots": 12, "unchanged_ratio": 0.75},
    )
    assert result.fallback is True
    assert result.error == "no_api_key"
    assert result.tokens_in == 0
    assert "本次计划调整" in result.text
    assert "goblet_squat" in result.text

def test_explain_parses_json(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"改了3处","why":"膝痛","risk":"注意疼痛","next":"确认后执行"}'
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    explainer = DeepSeekExplainer(
        api_key="test",
        base_url="https://example.com/v1",
        model="deepseek-chat",
        timeout_s=1,
        client=client,
    )
    result = explainer.explain(
        messages=[{"role": "user", "content": "膝痛"}],
        operations=[],
        diff={},
    )
    assert result.fallback is False
    assert "改动：改了3处" in result.text