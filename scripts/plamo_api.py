"""PLaMo Chat Completions primitives shared by weekly production and PoCs.

No week selection, file writes, retries or dependency on the weekly module here.
"""
import json
import urllib.request

MODEL = "plamo-3.1-prime"
ENDPOINT = "https://api.platform.preferredai.jp/v1/chat/completions"


class PlamoError(ValueError):
    """Fixed diagnostic codes only; never include provider response text."""

    def __init__(self, code, *, http_status=200):
        super().__init__(code)
        self.http_status = http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_request(system_prompt, user_prompt, schema, key, *, max_tokens, user_agent):
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "weekly_review", "strict": True, "schema": schema}},
            "max_tokens": max_tokens, "stream": False}
    return urllib.request.Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                           "Accept": "application/json", "User-Agent": user_agent})


def open_once(request, *, timeout=180):
    # Keep inherited proxy and TLS verification; never follow redirects.
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def call_api(request, *, timeout=180, max_response_bytes=1_000_000):
    with open_once(request, timeout=timeout) as response:
        if response.status != 200:
            raise PlamoError("unexpected_http_status", http_status=response.status)
        raw = response.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise PlamoError("response_too_large")
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise PlamoError("invalid_json") from error
    if not isinstance(data, dict) or data.get("error"):
        raise PlamoError("invalid_envelope")
    return data


def extract_text(data):
    if data.get("model") != MODEL:
        raise PlamoError("model_mismatch")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise PlamoError("invalid_choices")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
        raise PlamoError("incomplete_output")
    if message.get("refusal") or message.get("tool_calls") or message.get("function_call"):
        raise PlamoError("refusal_or_tool_call")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise PlamoError("missing_text")
    return text


def usage_tokens(data):
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {key: value for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(value := usage.get(key)) is int and value >= 0}
