#!/usr/bin/env python3
"""
npu_server.py — OpenAI-compatible local inference server via ONNX Runtime GenAI.

Targets the Qualcomm Hexagon NPU (QNNExecutionProvider / HTP backend) when the
loaded model's genai_config.json specifies it.  Falls back to CPU silently.

Requires (run setup_npu.py first):
    pip install onnxruntime-genai onnxruntime-qnn huggingface_hub

Usage:
    python npu_server.py --model ./models/phi-3.5-npu [--port 8123] [--template phi3]

Configure shellai.json:
    {
      "backend": "openai",
      "model": "phi-3.5-mini",
      "openai_compatible": { "base_url": "http://127.0.0.1:8123/v1", "api_key": "local" }
    }
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

try:
    import onnxruntime_genai as og
except ImportError:
    print(
        "\n[npu_server] onnxruntime-genai not installed.\n"
        "Run the launcher once to auto-install:  python launcher.py\n"
        "  or manually:  pip install onnxruntime-genai onnxruntime-directml\n",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Chat templates
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, Any] = {
    "phi3": {
        "system": "<|system|>\n{content}<|end|>\n",
        "user": "<|user|>\n{content}<|end|>\n",
        "assistant": "<|assistant|>\n{content}<|end|>\n",
        "start": "<|assistant|>\n",
        "stop": ["<|end|>", "<|endoftext|>"],
    },
    "qwen": {
        "system": "<|im_start|>system\n{content}<|im_end|>\n",
        "user": "<|im_start|>user\n{content}<|im_end|>\n",
        "assistant": "<|im_start|>assistant\n{content}<|im_end|>\n",
        "start": "<|im_start|>assistant\n",
        "stop": ["<|im_end|>", "<|endoftext|>"],
    },
    "llama3": {
        "system": "<|start_header_id|>system<|end_header_id|>\n\n{content}<|eot_id|>",
        "user": "<|start_header_id|>user<|end_header_id|>\n\n{content}<|eot_id|>",
        "assistant": "<|start_header_id|>assistant<|end_header_id|>\n\n{content}<|eot_id|>",
        "start": "<|start_header_id|>assistant<|end_header_id|>\n\n",
        "stop": ["<|eot_id|>", "<|end_of_text|>"],
    },
    "chatml": {
        "system": "<|im_start|>system\n{content}<|im_end|>\n",
        "user": "<|im_start|>user\n{content}<|im_end|>\n",
        "assistant": "<|im_start|>assistant\n{content}<|im_end|>\n",
        "start": "<|im_start|>assistant\n",
        "stop": ["<|im_end|>"],
    },
}


def _detect_template(model_path: Path) -> str:
    """Guess the chat template from the model directory name."""
    name = model_path.name.lower()
    if "phi-3" in name or "phi3" in name:
        return "phi3"
    if "qwen" in name:
        return "qwen"
    if "llama" in name:
        return "llama3"
    # Check tokenizer_config.json for chat_template hint
    tc = model_path / "tokenizer_config.json"
    if tc.exists():
        try:
            cfg = json.loads(tc.read_text())
            tpl = str(cfg.get("chat_template", ""))
            if "im_start" in tpl:
                return "chatml"
            if "start_header_id" in tpl:
                return "llama3"
        except Exception:
            pass
    return "chatml"


def apply_template(messages: list[dict[str, str]], template_name: str) -> str:
    tpl = TEMPLATES.get(template_name, TEMPLATES["chatml"])
    prompt = ""
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("system", "user", "assistant"):
            prompt += tpl[role].format(content=content)
    prompt += tpl["start"]
    return prompt


# ---------------------------------------------------------------------------
# Model wrapper
# ---------------------------------------------------------------------------

class LLMModel:
    def __init__(self, model_path: str, template: str) -> None:
        path = Path(model_path)
        if not path.exists():
            raise RuntimeError(f"Model directory not found: {path}")

        self.path = path
        self.template_name = template if template != "auto" else _detect_template(path)
        self.stop_tokens = TEMPLATES.get(self.template_name, TEMPLATES["chatml"])["stop"]
        self._lock = threading.Lock()

        print(f"  Loading model:    {path}")
        print(f"  Chat template:    {self.template_name}")

        self.model = og.Model(str(path))
        self.tokenizer = og.Tokenizer(self.model)

        # Show which execution provider is active
        self._print_provider_info(path)

    def _print_provider_info(self, path: Path) -> None:
        cfg_path = path / "genai_config.json"
        if not cfg_path.exists():
            print("  Execution provider: CPU (no genai_config.json found)")
            return
        try:
            cfg = json.loads(cfg_path.read_text())
            providers: list[str] = []
            decoder = cfg.get("model", {}).get("decoder", {})
            opts = decoder.get("session_options", {})
            for po in opts.get("provider_options", []):
                pname = po.get("provider_name", "")
                if pname:
                    providers.append(pname)
            if providers:
                using_dml = any("Dml" in p for p in providers)
                using_qnn = any("QNN" in p.upper() for p in providers)
                if using_qnn:
                    label = " \033[92m(Hexagon NPU — QNN/HTP active)\033[0m"
                elif using_dml:
                    label = " \033[92m(Adreno GPU — DirectML active)\033[0m"
                else:
                    label = ""
                print(f"  Execution providers: {', '.join(providers)}{label}")
            else:
                print("  Execution provider: CPU (default)")
        except Exception:
            print("  Execution provider: unknown (could not parse genai_config.json)")

    def generate(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> Iterator[str]:
        """Yield text tokens one at a time (generator). Thread-safe via lock."""
        prompt = apply_template(messages, self.template_name)

        with self._lock:
            input_tokens = self.tokenizer.encode(prompt)
            input_len = len(input_tokens)

            params = og.GeneratorParams(self.model)
            params.set_search_options(
                max_length=input_len + max_tokens,
                temperature=max(float(temperature), 0.01),
                top_p=0.9,
                do_sample=float(temperature) > 0.01,
            )
            params.input_ids = input_tokens

            generator = og.Generator(self.model, params)
            stream = self.tokenizer.create_stream()

            while not generator.is_done():
                generator.compute_logits()
                generator.generate_next_token()
                token_id = int(generator.get_next_tokens()[0])
                token_text = stream.decode(token_id)

                # Check for stop strings
                stop = False
                for s in self.stop_tokens:
                    if s in token_text:
                        token_text = token_text[: token_text.index(s)]
                        stop = True
                if token_text:
                    yield token_text
                if stop:
                    break

            # Clean up
            del generator


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

_model: LLMModel | None = None
_server_start = int(time.time())


def _req_id() -> str:
    return f"chatcmpl-{int(time.time() * 1000) % 10_000_000}"


class ChatHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {fmt % args}")

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/v1/model"):
            self._handle_models()
        elif self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path in ("/v1/chat/completions", "/chat/completions"):
            self._handle_chat()
        else:
            self.send_error(404)

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        return json.loads(raw) if raw else {}

    def _json_response(self, code: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_models(self) -> None:
        assert _model is not None
        self._json_response(200, {
            "object": "list",
            "data": [
                {
                    "id": _model.path.name,
                    "object": "model",
                    "created": _server_start,
                    "owned_by": "local",
                }
            ],
        })

    def _handle_chat(self) -> None:
        assert _model is not None
        try:
            req = self._read_body()
        except Exception as exc:
            self._json_response(400, {"error": str(exc)})
            return

        messages: list[dict[str, str]] = req.get("messages", [])
        temperature = float(req.get("temperature", 0.1))
        max_tokens = int(req.get("max_tokens", 1024))
        do_stream = bool(req.get("stream", False))

        if not messages:
            self._json_response(400, {"error": "messages is required"})
            return

        try:
            if do_stream:
                self._stream(_model, messages, temperature, max_tokens)
            else:
                self._complete(_model, messages, temperature, max_tokens)
        except Exception as exc:
            try:
                self._json_response(500, {"error": str(exc)})
            except Exception:
                pass

    def _complete(
        self,
        model: LLMModel,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> None:
        parts: list[str] = []
        for token in model.generate(messages, temperature, max_tokens):
            parts.append(token)
        content = "".join(parts)
        self._json_response(200, {
            "id": _req_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model.path.name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": -1,
                "completion_tokens": len(parts),
                "total_tokens": -1,
            },
        })

    def _stream(
        self,
        model: LLMModel,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> None:
        req_id = _req_id()
        created = int(time.time())
        model_name = model.path.name

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def _chunk(delta_content: str, finish: str | None = None) -> None:
            payload = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": (
                            {"role": "assistant", "content": delta_content}
                            if delta_content
                            else {}
                        ),
                        "finish_reason": finish,
                    }
                ],
            }
            line = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
            self.wfile.write(line)
            self.wfile.flush()

        try:
            _chunk("", None)  # opening chunk with role
            for token in model.generate(messages, temperature, max_tokens):
                _chunk(token, None)
            _chunk("", "stop")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as exc:
            try:
                err = f"data: {json.dumps({'error': str(exc)})}\n\n"
                self.wfile.write(err.encode())
                self.wfile.flush()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local OpenAI-compatible LLM server using ONNX Runtime GenAI (NPU/CPU)."
    )
    parser.add_argument(
        "--model", required=True,
        help="Path to an onnxruntime-genai model directory (contains genai_config.json).",
    )
    parser.add_argument("--port", type=int, default=8123, help="Port to listen on (default 8123).")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to.")
    parser.add_argument(
        "--template",
        choices=["auto", "phi3", "qwen", "llama3", "chatml"],
        default="auto",
        help="Chat template. 'auto' detects from model directory name.",
    )
    args = parser.parse_args()

    global _model
    print()
    print("  \033[1mNPU Server\033[0m  —  ONNX Runtime GenAI + QNN (Hexagon NPU / CPU fallback)")
    print()

    try:
        _model = LLMModel(args.model, args.template)
    except Exception as exc:
        print(f"\n[error] {exc}", file=sys.stderr)
        sys.exit(1)

    server = HTTPServer((args.host, args.port), ChatHandler)

    url = f"http://{args.host}:{args.port}/v1"
    print()
    print(f"  Server: \033[96m{url}\033[0m")
    print()
    print("  Update shellai.json:")
    print(json.dumps({
        "backend": "openai",
        "model": _model.path.name,
        "use_streaming": True,
        "openai_compatible": {
            "base_url": url,
            "api_key": "local",
        },
    }, indent=4))
    print()
    print("  Or run:  shellai --backend openai --model " + _model.path.name)
    print()
    print("  Press Ctrl-C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping server.")


if __name__ == "__main__":
    main()
