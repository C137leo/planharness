import httpx
import json
from app import config
from app.llm.fallback import fallback_explanation
from app.llm.prompts import build_messages


class DeepSeekExplainer:

    def __init__(self,*,api_key:str,base_url:str,model:str,timeout_s: float =8.0,client:httpx.Client|None=None):
        self.api_key=api_key
        self.base_url=base_url
        self.model=model
        self.timeout_s=timeout_s
        self._client = client or httpx.Client(timeout=timeout_s, trust_env=False)


    def available(self)-> bool:
        return bool(self.api_key and self.base_url)

    def explain(self,payload:dict,operations:list[dict], diff:dict) -> str:
        if not self.available():
            return fallback_explanation(operations, diff)
        try:
            messages = build_messages(payload)
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 300,
                "response_format": {"type": "json_object"},
            }
            raw = self.call_deepseek(body)
            return self.format_explanation(raw)
        except Exception:
            return fallback_explanation(operations,diff)




    def call_deepseek(self,body:dict) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        response = self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise RuntimeError(f"deepseek {response.status_code}: {response.text[:200]}")
        data = response.json()
        return (data["choices"][0]["message"].get("content") or "").strip()



    def format_explanation(self,raw: str) -> str:
        s = _strip_fence(raw)
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            # 兜底：模型偶尔会在 JSON 前后加一句解释，截取花括号区间再试一次
            l, r = s.find("{"), s.rfind("}")
            if l == -1 or r <= l:
                raise ValueError(f"no json object in response: {s[:120]!r}")
            data = json.loads(s[l:r + 1])

        if not isinstance(data, dict):
            raise ValueError(f"expected json object, got {type(data).__name__}")

        missing = [
            k for k in FIELDS
            if not isinstance(data.get(k), str) or not data[k].strip()
        ]
        if missing:
            raise ValueError(f"missing/empty fields: {missing}")

        return "\n".join(f"{LABELS[k]}：{data[k].strip()}" for k in FIELDS)

FIELDS = ("summary", "why", "risk", "next")
LABELS = {"summary": "改动", "why": "原因", "risk": "风险", "next": "下一步"}

def _strip_fence(raw: str) -> str:
    """去掉 ```json ... ``` 围栏；没有围栏就原样返回。"""
    s = raw.strip()
    if not s.startswith("```"):
        return s
    nl = s.find("\n")  # 跳过 ``` 或 ```json 这一行
    if nl == -1:  # 只有 ```xxx``` 单行
        return s.strip("`").strip()
    s = s[nl + 1:]
    if s.rstrip().endswith("```"):  # 去掉结尾围栏
        s = s.rstrip()[:-3]
    return s.strip()