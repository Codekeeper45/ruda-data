from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings


def _extract_json(value: str) -> Any:
    text_value = value.strip()
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.IGNORECASE)
        text_value = re.sub(r"\s*```$", "", text_value)
    try:
        return json.loads(text_value)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(text_value):
            if character not in "[{":
                continue
            try:
                data, _end = decoder.raw_decode(text_value[index:])
                return data
            except json.JSONDecodeError:
                continue
    raise ValueError("В ответе модели не найден корректный JSON")


class OpenRouterError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_account_rejection(self) -> bool:
        return self.status_code in {401, 402, 403}


@dataclass(slots=True)
class OpenRouterResult:
    data: Any
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw: dict[str, Any] | None = None


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://127.0.0.1:8000",
                "X-Title": settings.app_name,
            },
            timeout=httpx.Timeout(
                connect=30.0,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=30.0,
            ),
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any], *, retries: int = 5) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.client.post(f"{self.base_url}{path}", json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= retries:
                    break
                time.sleep(min(30.0, 2**attempt + random.random()))
                continue
            if not response.is_error:
                try:
                    data = response.json()
                except ValueError as exc:
                    error = OpenRouterError(
                        "OpenRouter временно вернул ответ не в JSON",
                        status_code=502,
                    )
                    last_error = error
                    if attempt + 1 < retries:
                        time.sleep(min(60.0, 2**attempt + random.random()))
                        continue
                    raise error from exc
                if not isinstance(data, dict):
                    raise OpenRouterError("OpenRouter вернул неожиданный формат ответа")
                embedded_error: Any = data.get("error")
                if embedded_error is None:
                    try:
                        choice = data["choices"][0]
                        embedded_error = choice.get("error")
                        if embedded_error is None:
                            content = (choice.get("message") or {}).get("content")
                            if isinstance(content, dict) and content.get("error"):
                                embedded_error = content["error"]
                            elif isinstance(content, str) and (
                                "ResourceExhausted" in content
                                or "Upstream error" in content
                            ):
                                embedded_error = {"message": content, "code": 502}
                    except (KeyError, IndexError, TypeError):
                        embedded_error = None
                if embedded_error is not None:
                    if isinstance(embedded_error, dict):
                        embedded_message = str(
                            embedded_error.get("message") or embedded_error
                        )
                        try:
                            embedded_code = int(embedded_error.get("code") or 502)
                        except (TypeError, ValueError):
                            embedded_code = 502
                    else:
                        embedded_message = str(embedded_error)
                        embedded_code = 502
                    error = OpenRouterError(
                        f"OpenRouter upstream {embedded_code}: {embedded_message[:1600]}",
                        status_code=embedded_code,
                    )
                    if (
                        embedded_code
                        in {408, 409, 429, 500, 502, 503, 504, 524, 529}
                        and attempt + 1 < retries
                    ):
                        last_error = error
                        time.sleep(min(60.0, 2**attempt + random.random()))
                        continue
                    raise error
                return data
            message = response.text[:2000]
            error = OpenRouterError(
                f"OpenRouter HTTP {response.status_code}: {message}",
                status_code=response.status_code,
            )
            if response.status_code in {401, 402, 403, 404}:
                raise error
            if response.status_code not in {408, 409, 429, 500, 502, 503, 504, 524, 529}:
                raise error
            last_error = error
            if attempt + 1 < retries:
                retry_after = response.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt + random.random()
                except ValueError:
                    delay = 2**attempt + random.random()
                time.sleep(min(60.0, max(1.0, delay)))
        raise OpenRouterError(f"OpenRouter недоступен после повторов: {last_error}")

    def structured_chat(
        self,
        *,
        model: str,
        system: str,
        user: Any,
        schema_name: str,
        schema: dict[str, Any],
        reasoning_effort: str = "medium",
        strict_schema: bool | None = None,
        max_tokens: int = 30_000,
    ) -> OpenRouterResult:
        if strict_schema is None:
            strict_schema = model not in {
                "inclusionai/ling-3.0-flash:free",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                "nvidia/nemotron-3-nano-30b-a3b:free",
                "nvidia/nemotron-nano-12b-v2-vl:free",
            }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning": {"effort": reasoning_effort},
        }
        if strict_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            schema_instruction = (
                "\n\nВерни только один корректный JSON-объект без Markdown. "
                "Он обязан соответствовать этой JSON Schema:\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            if isinstance(user, str):
                messages[-1]["content"] = user + schema_instruction
            elif isinstance(user, list):
                messages[-1]["content"] = [
                    {"type": "text", "text": schema_instruction},
                    *user,
                ]
        raw = self._post("/chat/completions", payload)
        try:
            content = raw["choices"][0]["message"]["content"]
            data = _extract_json(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
            message = ""
            try:
                message = str(raw["choices"][0]["message"].get("content") or "")[:1200]
            except (KeyError, IndexError, TypeError):
                message = str(raw)[:1200]
            raise OpenRouterError(
                f"Не удалось разобрать ответ {raw.get('model') or model}: {message}"
            ) from exc
        usage = raw.get("usage") or {}
        return OpenRouterResult(
            data=data,
            model=str(raw.get("model") or model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            estimated_cost_usd=float(usage.get("cost") or 0.0),
            raw=raw,
        )

    def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str,
        dimensions: int,
    ) -> OpenRouterResult:
        if not texts:
            return OpenRouterResult(data=[], model=model)
        raw = self._post(
            "/embeddings",
            {
                "model": model,
                "input": texts,
                "input_type": input_type,
                "dimensions": dimensions,
                "encoding_format": "float",
            },
        )
        try:
            ordered = sorted(raw["data"], key=lambda item: int(item["index"]))
            vectors = [item["embedding"] for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenRouterError(f"Не удалось разобрать эмбеддинги: {raw}") from exc
        usage = raw.get("usage") or {}
        return OpenRouterResult(
            data=vectors,
            model=str(raw.get("model") or model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=0,
            estimated_cost_usd=float(usage.get("cost") or 0.0),
            raw=raw,
        )
