"""A stand-in for vLLM: an OpenAI-compatible server that answers every request with the smallest
JSON its schema allows (first enum value, empty strings and lists, minimum numbers).

It knows nothing, so its answers measure nothing. It exists to dry-run the pipeline: the overnight
script, the evaluation, the gate, the cache, without a GPU. Because it honours the schema, it passes
the smoke check, which is what a real constrained server must do too.

    python -m dmgate.llm.stub_server --port 8000 --model Qwen/Qwen3.5-4B
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def minimal(schema: dict[str, Any]) -> Any:
    """The smallest value that fits `schema` (the subset `dmgate.llm.jsonschema` checks)."""
    if "anyOf" in schema:
        return minimal(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) else kind
    if kind == "object":
        properties = schema.get("properties", {})
        return {name: minimal(properties[name]) for name in schema.get("required", ()) if name in properties}
    if kind == "array":
        return [minimal(schema.get("items", {})) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        return "x" * schema.get("minLength", 0)
    if kind in ("integer", "number"):
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return None


def schema_of(body: dict[str, Any]) -> dict[str, Any]:
    response_format = body.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        return response_format["json_schema"]["schema"]
    return (body.get("structured_outputs") or {}).get("json") or {}


class StubHandler(BaseHTTPRequestHandler):
    model = "stub"

    def _send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path.rstrip("/").endswith("/models"):
            self._send({"object": "list", "data": [{"id": self.model, "object": "model"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        answer = json.dumps(minimal(schema_of(body)))
        choices = [{"index": i, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}
                   for i in range(int(body.get("n", 1)))]
        self._send({"model": body.get("model", self.model), "choices": choices})

    def log_message(self, *args: Any) -> None:
        pass


def serve(port: int, model: str, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("Handler", (StubHandler,), {"model": model})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="A schema-minimal stand-in for an OpenAI-compatible server.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default="stub")
    args = parser.parse_args(argv)
    server = serve(args.port, args.model)
    print(f"stub server for {args.model} on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
