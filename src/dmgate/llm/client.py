"""OpenAI-compatible chat client with constrained JSON output.

vLLM serves an OpenAI-compatible API (`vllm serve <model>`). Asking for
`response_format = {"type": "json_schema", ...}` makes it constrain decoding, token by token, to
JSON that fits the schema (structured outputs). `structured="guided_json"` uses vLLM's older
parameter instead. Whatever the server does, every output is parsed and checked against the schema
again here; an output that fails is returned as None, never guessed at.

`CachedClient` stores every answer on disk, keyed by the exact request, so evaluations replay
bit for bit. `ScriptedClient` answers from a function, for tests.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from dmgate.llm.jsonschema import errors

Messages = list[dict[str, str]]


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    outputs: tuple[dict[str, Any] | None, ...]  # one per sample; None if it did not parse or fit the schema
    raw: tuple[str, ...]
    model: str
    latency_ms: float = 0.0
    cached: bool = False

    @property
    def valid(self) -> list[dict[str, Any]]:
        return [o for o in self.outputs if o is not None]


class LLMClient(Protocol):
    model: str

    def complete_json(self, messages: Messages, schema: dict[str, Any], *, name: str = "answer", n: int = 1,
                      temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None) -> Completion: ...


def _parse(text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if not errors(value, schema) else None


class OpenAIClient:
    def __init__(self, base_url: str, model: str, *, api_key: str | None = None, timeout: float = 120.0,
                 structured: str = "response_format", extra_body: dict[str, Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.structured = structured
        self.extra_body = extra_body or {}

    def request_body(self, messages: Messages, schema: dict[str, Any], *, name: str, n: int, temperature: float,
                     max_tokens: int, seed: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "n": n, "temperature": temperature,
                                "max_tokens": max_tokens}
        if self.structured == "guided_json":
            body["guided_json"] = schema
        else:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": name, "schema": schema, "strict": True}}
        if seed is not None:
            body["seed"] = seed
        return {**body, **self.extra_body}

    def complete_json(self, messages: Messages, schema: dict[str, Any], *, name: str = "answer", n: int = 1,
                      temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None) -> Completion:
        body = self.request_body(messages, schema, name=name, n=n, temperature=temperature, max_tokens=max_tokens,
                                 seed=seed)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                reply = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise LLMError(f"{error.code} from {self.base_url}: {error.read().decode('utf-8', 'replace')[:500]}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise LLMError(f"cannot reach {self.base_url}: {error}") from error
        texts = tuple((choice.get("message") or {}).get("content") or "" for choice in reply.get("choices", []))
        return Completion(tuple(_parse(t, schema) for t in texts), texts, reply.get("model", self.model),
                          (time.perf_counter() - start) * 1000.0)


class CachedClient:
    """Answers repeated requests from a JSONL file, so an evaluation can be replayed exactly."""

    def __init__(self, inner: LLMClient, path: Path):
        self.inner = inner
        self.model = inner.model
        self.path = path
        self._cache: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._cache[entry["key"]] = entry

    @staticmethod
    def key(model: str, messages: Messages, schema: dict[str, Any], **params: Any) -> str:
        blob = json.dumps({"model": model, "messages": messages, "schema": schema, **params}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete_json(self, messages: Messages, schema: dict[str, Any], *, name: str = "answer", n: int = 1,
                      temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None) -> Completion:
        key = self.key(self.model, messages, schema, name=name, n=n, temperature=temperature, max_tokens=max_tokens,
                       seed=seed)
        if key in self._cache:
            raw = tuple(self._cache[key]["raw"])
            return Completion(tuple(_parse(t, schema) for t in raw), raw, self._cache[key]["model"], cached=True)
        completion = self.inner.complete_json(messages, schema, name=name, n=n, temperature=temperature,
                                              max_tokens=max_tokens, seed=seed)
        entry = {"key": key, "model": completion.model, "raw": list(completion.raw)}
        self._cache[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return completion


@dataclass
class ScriptedClient:
    """Answers with `respond(messages, schema, name)`, which returns one output (or a list, one per
    sample). For tests and plumbing checks; it records every call."""

    respond: Callable[[Messages, dict[str, Any], str], Any]
    model: str = "scripted"
    calls: list[tuple[str, Messages]] = field(default_factory=list)

    def complete_json(self, messages: Messages, schema: dict[str, Any], *, name: str = "answer", n: int = 1,
                      temperature: float = 0.0, max_tokens: int = 512, seed: int | None = None) -> Completion:
        self.calls.append((name, messages))
        answer = self.respond(messages, schema, name)
        answers = answer if isinstance(answer, list) else [answer] * n
        raw = tuple(json.dumps(a) for a in answers[:n])
        return Completion(tuple(_parse(t, schema) for t in raw), raw, self.model)
