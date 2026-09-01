#!/usr/bin/env python3
"""hexcli.llm — model transport: streaming, non-streaming, the mock backend,
and the token estimator. Lifted out of agent.py.

call_llm is the single entry point the loop uses; agent.py re-binds it (and
everything else here), so patching sa.call_llm still intercepts every model
call in the loop, in loop_v2, and in compaction.

Two things deliberately resolve through the agent hub at call time rather
than locally:
  * _CURRENT_SESSION_ID — run_autopilot owns it, and npurun uses it to detect
    continuation turns and skip a dialog reset. Reading a module-local copy
    here would silently break that (and the tests that patch it).
  * the cancellation primitives, via agent's re-bound names, so the eval
    runner's silencers apply.

_MOCK_RESPONSE_QUEUE is mutated in place (never rebound), so agent's alias
stays live for the suites that assert on queue depth.

Split stage 7 (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim
apart from the two hub lookups.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.error
from typing import Any

from hexcli import memory
from hexcli.http_client import http_json_request
from hexcli.ui import C


def _agent():
    from hexcli import agent
    return agent


def __getattr__(name: str) -> Any:
    """Fall back to the agent module for the handful of names this layer
    borrows (Spinner, CancelMonitor, _agent().UserCancelled, DEBUG, ...), so the eval
    runner's patches on hexcli.agent are honoured at call time."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(_agent(), name)


_MOCK_RESPONSE_QUEUE: list[str] = []


def set_mock_responses(responses: list[str]) -> None:
    """Load scripted LLM responses. Each call to call_llm pops the next entry.

    Fixture entries are raw strings — identical to what a real LLM would return
    (JSON action objects, finish messages, plain text, etc.).
    """
    _MOCK_RESPONSE_QUEUE[:] = responses


def _pop_mock_response() -> tuple[str, int]:
    """Return (response_text, eval_count); falls back to a finish action."""
    if _MOCK_RESPONSE_QUEUE:
        return (_MOCK_RESPONSE_QUEUE.pop(0), 0)
    return ('{"action":"finish","message":"Mock queue exhausted."}', 0)


class _TokenEstimator:
    """Data-driven replacement for the blanket chars/4 token estimate.

    Every live completion returns an exact token count (the fork emits one
    Genie chunk per generated token), and the text length is known locally —
    so the real chars-per-token ratio of THIS model on THIS workload is
    observable for free. The estimate feeds the context budget, where assuming
    4 chars/token while code-heavy turns actually run ~3.3 means firing
    compaction PAST the ~2,600-token degradation cliff — the v1.7 calibration
    bug one layer down.

    EMA over completions, clamped so one garbage usage report cannot poison
    the budget. Starts at 4.0, which is byte-for-byte the old behaviour until
    real observations arrive. A lower ratio means a HIGHER token estimate and
    therefore earlier compaction — the safe direction.
    """

    def __init__(self) -> None:
        self.ratio = 4.0
        self.observations = 0

    def observe(self, chars: int, tokens: int) -> None:
        if tokens < 20 or chars < 40:
            return  # too small to carry signal
        sample = chars / tokens
        if not 1.5 <= sample <= 8.0:
            return  # implausible; likely a broken usage report
        self.ratio = min(4.5, max(2.5, 0.9 * self.ratio + 0.1 * sample))
        self.observations += 1

    def estimate(self, text_len: int) -> int:
        return int(text_len / self.ratio)


_TOKEN_ESTIMATOR = _TokenEstimator()


def estimate_tokens(text: str) -> int:
    """Estimated token count of `text` for budget decisions."""
    return _TOKEN_ESTIMATOR.estimate(len(text))


def _ollama_stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Stream from Ollama /api/chat. Returns (content, eval_count)."""
    host = config["ollama"]["host"].rstrip("/")
    url = f"{host}/api/chat"
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get(token_key, 2048)),
        },
    }
    if json_format:
        payload["format"] = "json"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")

    line_q: queue.Queue[bytes | None] = queue.Queue()
    err_box: dict[str, BaseException] = {}

    def read_lines(resp: Any) -> None:
        try:
            for raw in resp:
                line_q.put(raw)
        except BaseException as exc:  # noqa: BLE001
            err_box["value"] = exc
        finally:
            line_q.put(None)

    parts: list[str] = []
    eval_count = 0
    tok = 0

    # A dedicated connection per call, not the shared keep-alive pool used by
    # the non-streaming helpers below: the response body here is read by a
    # background thread and can be abandoned mid-stream (cancel, or the
    # "done" line arriving before the socket reaches EOF), which would leave
    # a shared connection in an indeterminate state for the next reuse.
    try:
        with urllib.request.urlopen(req, timeout=int(config["timeout_seconds"])) as resp:
            reader = threading.Thread(target=read_lines, args=(resp,), daemon=True)
            with _agent().CancelMonitor() as monitor:
                reader.start()
                while True:
                    if monitor.cancelled.is_set():
                        raise _agent().UserCancelled()
                    try:
                        raw = line_q.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if raw is None:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = (data.get("message") or {}).get("content", "")
                    if chunk:
                        parts.append(chunk)
                        tok += 1
                        sys.stderr.write(
                            f"\r{C.DIM}  {label}... {tok} tokens  (Esc to cancel){C.RESET}"
                        )
                        sys.stderr.flush()
                    if data.get("done"):
                        eval_count = data.get("eval_count", tok)

        if "value" in err_box:
            exc = err_box["value"]
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                sys.stderr.write(f"\r{C.YELLOW}  stream dropped — retrying …{C.RESET}          \n")
                sys.stderr.flush()
                return ollama_chat_non_stream(config, messages, token_key, json_format=json_format), 0
            raise exc

        return "".join(parts), eval_count
    finally:
        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()


def ollama_chat_non_stream(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    json_format: bool = False,
) -> str:
    host = config["ollama"]["host"].rstrip("/")
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get(token_key, 2048)),
        },
    }
    if json_format:
        payload["format"] = "json"
    resp = http_json_request(f"{host}/api/chat", payload, {}, int(config["timeout_seconds"]))
    return str((resp.get("message") or {}).get("content", "")).strip()


def openai_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    json_format: bool = False,
) -> str:
    base_url = config["openai_compatible"]["base_url"].rstrip("/")
    api_key = config["openai_compatible"].get("api_key", "local")
    payload: dict[str, Any] = {
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": int(config.get(token_key, 2048)),
        "messages": messages,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}
    if _agent()._CURRENT_SESSION_ID is not None:
        payload["session_id"] = _agent()._CURRENT_SESSION_ID
    resp = http_json_request(
        f"{base_url}/chat/completions", payload,
        {"Authorization": f"Bearer {api_key}"}, int(config["timeout_seconds"])
    )
    choices = resp.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI-compatible backend returned no choices.")
    return str((choices[0].get("message") or {}).get("content", "")).strip()


def _openai_stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Stream from an OpenAI-compatible SSE endpoint. Returns (content, token_count).

    SSE format (per chunk):  data: {"choices":[{"delta":{"content":"..."},...}]}
    Terminator:              data: [DONE]
    """
    base_url = config["openai_compatible"]["base_url"].rstrip("/")
    api_key = config["openai_compatible"].get("api_key", "local")
    url = f"{base_url}/chat/completions"

    payload: dict[str, Any] = {
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": int(config.get(token_key, 2048)),
        "messages": messages,
        "stream": True,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}
    if _agent()._CURRENT_SESSION_ID is not None:
        payload["session_id"] = _agent()._CURRENT_SESSION_ID
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    line_q: queue.Queue[bytes | None] = queue.Queue()
    err_box: dict[str, BaseException] = {}

    def read_lines(resp: Any) -> None:
        try:
            for raw in resp:
                line_q.put(raw)
        except BaseException as exc:  # noqa: BLE001
            err_box["value"] = exc
        finally:
            line_q.put(None)

    parts: list[str] = []
    tok = 0
    renderer = _make_live_renderer(config, label)

    # Dedicated per-call connection — see _ollama_stream_chat for why the
    # shared keep-alive pool isn't used here.
    try:
        with urllib.request.urlopen(req, timeout=int(config["timeout_seconds"])) as resp:
            reader = threading.Thread(target=read_lines, args=(resp,), daemon=True)
            with _agent().CancelMonitor() as monitor:
                reader.start()
                while True:
                    if monitor.cancelled.is_set():
                        raise _agent().UserCancelled()
                    try:
                        raw = line_q.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if raw is None:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    # SSE lines start with "data: "
                    text = line.decode("utf-8", errors="replace")
                    if text == "data: [DONE]":
                        break
                    if not text.startswith("data: "):
                        continue
                    try:
                        data = json.loads(text[6:])
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content", "")
                    if delta:
                        parts.append(delta)
                        tok += 1
                        if renderer is not None:
                            renderer.feed(delta)
                        else:
                            sys.stderr.write(
                                f"\r{C.DIM}  {label}... {tok} tokens  (Esc to cancel){C.RESET}"
                            )
                            sys.stderr.flush()

        if "value" in err_box:
            exc = err_box["value"]
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                sys.stderr.write(f"\r{C.YELLOW}  stream dropped — retrying …{C.RESET}          \n")
                sys.stderr.flush()
                return openai_chat(config, messages, token_key, json_format=json_format), 0
            raise exc

        if renderer is not None:
            renderer.finish()
        return "".join(parts), tok
    finally:
        if renderer is not None:
            _end_live_render(renderer)
        else:
            sys.stderr.write("\r" + " " * 60 + "\r")
            sys.stderr.flush()


def _make_live_renderer(config: dict[str, Any], label: str) -> Any:
    """Renderer that prints the answer as it arrives, or None when live
    rendering is off / inappropriate (evals, delegate sub-loops, compaction).

    v1.7 showed only a token counter, so a 20-90s answer looked like a hang
    (review finding W6). The renderer streams the finish message's TEXT and
    announces tool intent early, without ever showing raw JSON.
    """
    if not config.get("live_streaming", True):
        return None
    if _agent()._in_delegate or label in ("compacting", "summarising"):
        return None
    if not sys.stderr.isatty():
        return None  # eval/CI capture: keep logs clean

    from .stream_render import StreamRenderer

    state = {"started": False}

    def emit(text: str) -> None:
        if not state["started"]:
            sys.stderr.write("\r" + " " * 60 + "\r")
            state["started"] = True
        sys.stdout.write(text)
        sys.stdout.flush()

    def on_tool(name: str) -> None:
        sys.stderr.write(f"\r{C.DIM}  → {name}{C.RESET}" + " " * 20)
        sys.stderr.flush()

    r = StreamRenderer(emit, on_tool)
    r._live_started = state  # type: ignore[attr-defined]
    return r


def _end_live_render(renderer: Any) -> None:
    started = getattr(renderer, "_live_started", {}).get("started", False)
    if started:
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        sys.stderr.write("\r" + " " * 60 + "\r")
        sys.stderr.flush()


def ollama_generate_with_system(config: dict[str, Any], system: str, prompt: str) -> str:
    host = config["ollama"]["host"].rstrip("/")
    payload: dict[str, Any] = {
        "model": config["model"],
        "system": system,
        "prompt": prompt.strip(),
        "stream": False,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get("max_output_tokens", 512)),
        },
    }
    resp = http_json_request(f"{host}/api/generate", payload, {}, int(config["timeout_seconds"]))
    return str(resp.get("response", "")).strip()


def openai_generate_with_system(config: dict[str, Any], system: str, prompt: str) -> str:
    return openai_chat(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_output_tokens",
    )


def llm_generate(config: dict[str, Any], system: str, prompt: str) -> str:
    if config.get("backend") == "mock":
        return _pop_mock_response()[0]
    if config["backend"] == "ollama":
        return ollama_generate_with_system(config, system, prompt)
    if config["backend"] == "openai":
        return openai_generate_with_system(config, system, prompt)
    raise RuntimeError(f"Unsupported backend: {config['backend']}")


def call_llm(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    *,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Unified LLM call with correct cancellation.

    Streaming path (_ollama_stream_chat) manages its own CancelMonitor; calling
    it through run_cancellable would create two competing monitors on the same
    console input buffer. Non-streaming path uses run_cancellable + Spinner.
    Acquires memory._NPU_INFERENCE_LOCK so the dreaming daemon defers while any
    inference is in progress.
    """
    with memory._NPU_INFERENCE_LOCK:
        if config.get("backend") == "mock":
            # Mock fixtures carry no real token counts — never feed the
            # estimator from them.
            return _pop_mock_response()

        if config["backend"] == "ollama" and config.get("use_streaming", True):
            text, count = _ollama_stream_chat(
                config, messages, token_key, label=label, json_format=json_format)
            _TOKEN_ESTIMATOR.observe(len(text), count)
            return text, count

        if config["backend"] == "openai" and config.get("use_streaming", True):
            text, count = _openai_stream_chat(
                config, messages, token_key, label=label, json_format=json_format)
            _TOKEN_ESTIMATOR.observe(len(text), count)
            return text, count

        result_box: dict[str, Any] = {}
        error_box: dict[str, BaseException] = {}

        def _work() -> None:
            try:
                if config["backend"] == "ollama":
                    content = ollama_chat_non_stream(config, messages, token_key, json_format=json_format)
                elif config["backend"] == "openai":
                    content = openai_chat(config, messages, token_key, json_format=json_format)
                else:
                    raise RuntimeError(f"Unsupported backend: {config['backend']}")
                result_box["value"] = (content, 0)
            except BaseException as exc:  # noqa: BLE001
                error_box["value"] = exc

        thread = threading.Thread(target=_work, daemon=True)
        with _agent().CancelMonitor() as monitor, _agent().Spinner(f"{label} (Esc to cancel)"):
            thread.start()
            while thread.is_alive():
                if monitor.cancelled.is_set():
                    raise _agent().UserCancelled()
                thread.join(0.05)

        if "value" in error_box:
            raise error_box["value"]
        return result_box.get("value", ("", 0))

