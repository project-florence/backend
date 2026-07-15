from openai import OpenAI
from src.core.config import get_config

_client = None
_default_model = None


def init_client(url=None, default_model=None, api_key=None):
    global _client, _default_model
    cfg = get_config()
    llm_cfg = cfg.get("llm_client", {})
    if url is None:
        url = llm_cfg.get("url")
    if api_key is None:
        api_key = llm_cfg.get("api_key")
    _client = OpenAI(api_key=api_key, base_url=url)
    _default_model = default_model or llm_cfg.get("model")


def get_response(
    prompt: str,
    role: str = "user",
    model: str = None,
    tools: list[dict] | None = None,
    messages: list[dict] | None = None,
) -> dict:
    global _client, _default_model
    if _client is None:
        init_client()
    if model is None:
        model = _default_model
        if model is None:
            raise ValueError("No model provided")

    if messages is None:
        messages = [{"role": role, "content": prompt}]

    kwargs = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools

    try:
        import json
        response = _client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        usage_dict = {
            "prompt": usage.prompt_tokens,
            "completion": usage.completion_tokens,
            "total": usage.total_tokens,
            "model": getattr(response, "model", model),
        } if usage else None

        if choice.finish_reason == "tool_calls":
            calls = []
            for tc in choice.message.tool_calls:
                calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })
            return {
                "type": "tool_calls",
                "calls": calls,
                "usage": usage_dict,
                "assistant_message": choice.message,
            }

        return {"type": "text", "content": choice.message.content, "usage": usage_dict}
    except Exception as e:
        return {"type": "error", "detail": str(e), "usage": None}


def health_check():
    global _client
    try:
        if _client is None:
            init_client()
        response = _client.models.list()
        return response is not None
    except Exception:
        return False
