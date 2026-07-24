from openai import OpenAI
from src.core.config import get_config
from dotenv import load_dotenv
import os

load_dotenv()

_client = None
_default_model = None
_client_type = None


def init_client(url=None, default_model=None, api_key=None):
    global _client, _default_model, _client_type
    cfg = get_config()
    llm_cfg = cfg.get("llm_client", {})
    _client_type = llm_cfg.get("type", "custom")

    if _client_type == "openrouter":
        base_url = url or os.getenv("OPENROUTER_URL") or llm_cfg.get("openrouter_url")
        key = api_key or os.getenv("OPENROUTER_API_KEY")
        _default_model = default_model or "openrouter/free"
    else:
        base_url = url or os.getenv("CUSTOM_URL") or llm_cfg.get("custom_url")
        key = api_key or os.getenv("CUSTOM_API_KEY") or llm_cfg.get("api_key", "")
        _default_model = default_model or os.getenv("CUSTOM_MODEL") or llm_cfg.get("custom_model")

    _client = OpenAI(api_key=key, base_url=base_url)


def get_response(
    prompt: str,
    role: str = "user",
    model: str = None,
    tools: list[dict] | None = None,
    messages: list[dict] | None = None,
    reasoning: bool = False,
) -> dict:
    global _client, _default_model, _client_type
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
    if reasoning and _client_type == "openrouter":
        kwargs["extra_body"] = {"reasoning": {"enabled": True}}

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

        result = {"type": "text", "content": choice.message.content, "usage": usage_dict}

        reasoning_details = getattr(choice.message, "reasoning_details", None)
        if not reasoning_details and hasattr(choice.message, "model_extra"):
            reasoning_details = choice.message.model_extra.get("reasoning_details")
        if reasoning_details:
            result["reasoning_details"] = reasoning_details

        return result
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
