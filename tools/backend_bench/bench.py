"""CPU-vs-NPU backend study harness for Hex CLI.

Runs identical chat workloads against three servers while `typeperf` logs the
laptop's hardware energy meters (power AND cumulative energy), CPU utilisation,
clocks, thermal zones and per-server process counters at 1 Hz:

  npu      npurun serve (Genie/QNN HTP), OpenAI-compatible on :11435, session_id → Rewind
  cpu      Ollama, native /api/chat on :11434 (what Hex's `backend: ollama` gets)
  llamacpp upstream llama-server, OpenAI-compatible on :8080

Every request is appended to <out>/requests.jsonl with wall-clock bounds so the
counter CSVs can be joined afterwards (analyze.py). Stdlib only (repo rule).
Token counts come from llama-tokenize on the GGUF's Qwen tokenizer, so all
backends are counted with the same tokenizer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

BASES = {"npu": "http://127.0.0.1:11435", "cpu": "http://127.0.0.1:11434", "llamacpp": "http://127.0.0.1:8080"}
LLAMA_TOKENIZE = Path(r"C:\Users\Natha\Tools\llama-cpp\cpu\llama-tokenize.exe")

# Main 1 Hz counter set. The NPU engine counter is sampled separately (see _npu_sampler).
COUNTERS = [
    r"\Energy Meter(*)\Power",
    r"\Energy Meter(*)\Energy",
    r"\Processor Information(_Total)\% Processor Utility",
    r"\Processor Information(_Total)\% Processor Time",
    r"\Processor Information(_Total)\Processor Frequency",
    r"\Thermal Zone Information(\_SB.TZ0)\High Precision Temperature",
    r"\Thermal Zone Information(\_SB.TZ2)\High Precision Temperature",
    r"\Thermal Zone Information(\_SB.TZ98)\High Precision Temperature",
    r"\Process(npurun)\% Processor Time",
    r"\Process(npurun)\Working Set - Private",
    r"\Process(llama-server*)\% Processor Time",      # Ollama's runner AND upstream llama-server
    r"\Process(llama-server*)\Working Set - Private",
    r"\Process(ollama*)\% Processor Time",            # Ollama supervisor (small)
    r"\Memory\Available MBytes",
    r"\System\Processor Queue Length",
]

WORDS = ("the system reads a file then writes a summary of every function it found "
         "while the user waits for the terminal to print the next line of output "
         "network storage memory kernel driver process thread socket buffer cache "
         "index table column row query result error retry timeout budget window").split()

NUM_THREAD: int | None = None  # Ollama num_thread override (None → Ollama default, 6 here)


# ---------------------------------------------------------------------------
# NPU adapter discovery (LUIDs are reassigned at every boot)
# ---------------------------------------------------------------------------
def discover_npu_luid() -> str:
    """The NPU is the GPU-Engine adapter that DirectX does not register (the GPU and
    the Basic Render Driver are). It exposes a single Compute engine (MCDM device)."""
    import winreg
    known: set[str] = set()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX") as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                    v, _ = winreg.QueryValueEx(k, "AdapterLuid")
                    known.add(f"0x{(v >> 32) & 0xFFFFFFFF:08X}_0x{v & 0xFFFFFFFF:08X}")
            except OSError:
                pass
    out = subprocess.run(["typeperf", "-qx", r"\GPU Engine"], capture_output=True, text=True, timeout=60).stdout
    by_luid: dict[str, set[str]] = {}
    for luid, eng in re.findall(r"luid_(0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_\d+_eng_\d+_engtype_(\w+)", out):
        by_luid.setdefault(luid.upper(), set()).add(eng)
    rest = [luid for luid, engs in by_luid.items() if luid not in known]
    rest.sort(key=lambda luid: by_luid[luid] != {"Compute"})
    if not rest:
        raise RuntimeError(f"no NPU adapter among GPU Engine counters; known={sorted(known)} seen={sorted(by_luid)}")
    return rest[0]


# ---------------------------------------------------------------------------
# tokenizer (shared for all backends)
# ---------------------------------------------------------------------------
_TOK_CACHE: dict[str, int] = {}


def gguf_path(model_tag: str) -> Path:
    root = Path.home() / ".ollama" / "models"
    if model_tag.startswith("hf.co/"):
        repo, _, tag = model_tag[len("hf.co/"):].partition(":")
        mf = root / "manifests" / "hf.co" / repo / (tag or "latest")
    else:
        name, _, tag = model_tag.partition(":")
        mf = root / "manifests" / "registry.ollama.ai" / "library" / name / (tag or "latest")
    m = json.loads(mf.read_text())
    for layer in m["layers"]:
        if layer["mediaType"].endswith("image.model"):
            return root / "blobs" / layer["digest"].replace(":", "-")
    raise RuntimeError(f"no model layer in {mf}")


def count_tokens(text: str, gguf: Path) -> int:
    key = f"{len(text)}:{hash(text)}"
    if key in _TOK_CACHE:
        return _TOK_CACHE[key]
    tmp = HERE / f".tok_{os.getpid()}.txt"
    tmp.write_text(text, encoding="utf-8")
    try:
        out = subprocess.run([str(LLAMA_TOKENIZE), "-m", str(gguf), "-f", str(tmp), "--ids", "--log-disable"],
                             capture_output=True, text=True, timeout=120)
        ids_line = [ln for ln in out.stdout.splitlines() if ln.startswith("[")][-1]
        n = ids_line.count(",") + 1 if ids_line.strip() != "[]" else 0
    finally:
        tmp.unlink(missing_ok=True)
    _TOK_CACHE[key] = n
    return n


def chatml(messages: list[dict]) -> str:
    s = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)
    return s + "<|im_start|>assistant\n"


def make_text(n_tokens: int, gguf: Path, seed: int) -> str:
    """Natural-looking filler of approximately n_tokens tokens (±2%)."""
    rng = random.Random(seed)
    words = []
    while len(words) < int(n_tokens * 0.8):
        words.append(rng.choice(WORDS) + ("." if rng.random() < 0.08 else ""))
    text = " ".join(words)
    for _ in range(6):
        t = count_tokens(text, gguf)
        if abs(t - n_tokens) <= max(2, n_tokens // 50):
            break
        ratio = n_tokens / max(t, 1)
        if ratio < 1:
            words = words[: int(len(words) * ratio)]
        else:
            words += [rng.choice(WORDS) for _ in range(int(len(words) * (ratio - 1)) + 1)]
        text = " ".join(words)
    return text


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
class Reply:
    def __init__(self) -> None:
        self.t0 = 0.0
        self.t_first = 0.0
        self.t_end = 0.0
        self.text = ""
        self.chunks = 0
        self.server: dict = {}
        self.error = ""
        self.finish_reason = ""
        self.busy_wait_s = 0.0
        self.busy_retries = 0


def _post_stream(url: str, payload: dict, timeout: int, on_line) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer local")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            on_line(raw.decode("utf-8", "replace").strip())


def chat_openai(base: str, model: str, messages: list[dict], max_tokens: int,
                session_id: str | None, temperature: float, timeout: int = 900) -> Reply:
    r = Reply()
    payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
               "messages": messages, "stream": True, "stop": ["<|im_end|>", "<|im_start|>"]}
    if session_id:
        payload["session_id"] = session_id

    def on_line(line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return
        if obj.get("usage"):
            r.server["usage"] = obj["usage"]
        if obj.get("timings"):
            r.server["timings"] = obj["timings"]
        for ch in obj.get("choices", []):
            piece = (ch.get("delta") or {}).get("content") or ""
            if piece:
                if not r.t_first:
                    r.t_first = time.perf_counter()
                r.text += piece
                r.chunks += 1
            if ch.get("finish_reason"):
                r.finish_reason = ch["finish_reason"]

    r.t0 = time.perf_counter()
    t_busy = 0.0
    for _ in range(60):
        t_try = time.perf_counter()
        try:
            _post_stream(f"{base}/v1/chat/completions", payload, timeout, on_line)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:            # npurun: single inference slot busy (prewarm / previous request)
                t_busy += time.perf_counter() - t_try
                r.busy_retries += 1
                time.sleep(0.3)
                continue
            r.error = f"HTTP {e.code}: {e.read()[:200]!r}"
            break
        except Exception as e:  # noqa: BLE001
            r.error = repr(e)
            break
    r.t_end = time.perf_counter()
    r.busy_wait_s = t_busy + 0.3 * r.busy_retries
    return r


def chat_ollama(base: str, model: str, messages: list[dict], max_tokens: int,
                num_ctx: int, temperature: float, timeout: int = 900) -> Reply:
    r = Reply()
    payload = {"model": model, "messages": messages, "stream": True, "keep_alive": -1,
               "options": {"temperature": temperature, "num_predict": max_tokens,
                           "num_ctx": num_ctx, "stop": ["<|im_end|>", "<|im_start|>"]}}
    if NUM_THREAD:
        payload["options"]["num_thread"] = NUM_THREAD

    def on_line(line: str) -> None:
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        piece = (obj.get("message") or {}).get("content") or ""
        if piece:
            if not r.t_first:
                r.t_first = time.perf_counter()
            r.text += piece
            r.chunks += 1
        if obj.get("done"):
            r.finish_reason = obj.get("done_reason", "")
            r.server = {k: obj.get(k) for k in ("prompt_eval_count", "prompt_eval_duration",
                                                "eval_count", "eval_duration",
                                                "load_duration", "total_duration")}

    r.t0 = time.perf_counter()
    try:
        _post_stream(f"{base}/api/chat", payload, timeout, on_line)
    except Exception as e:  # noqa: BLE001
        r.error = repr(e)
    r.t_end = time.perf_counter()
    return r


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
class Bench:
    def __init__(self, backend: str, model: str, out: Path, gguf: Path, num_ctx: int,
                 temperature: float, tag: str) -> None:
        self.backend, self.model, self.out, self.gguf = backend, model, out, gguf
        self.num_ctx, self.temperature, self.tag = num_ctx, temperature, tag
        self.base = BASES[backend]
        out.mkdir(parents=True, exist_ok=True)
        self.req_log = out / "requests.jsonl"
        self._lock = threading.Lock()
        self.typeperf: subprocess.Popen | None = None
        self._epoch_offset = time.time() - time.perf_counter()
        self.npu_luid = ""
        self._npu_fail = 0
        self._npu_files = 0
        self._sys_prompt_cache: str | None = None

    # -- counters -----------------------------------------------------------
    def start_counters(self) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv = self.out / f"counters_{self.backend}_{self.tag}_{stamp}.csv"
        self.typeperf = subprocess.Popen(["typeperf", "-si", "1", "-y", "-o", str(csv)] + COUNTERS,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.counter_csv = csv
        self.npu_luid = discover_npu_luid()
        self.npu_counter = r"\GPU Engine(*luid_" + self.npu_luid + r"*)\Utilization Percentage"
        # The NPU engine counter instance is per device context: Genie recreating its
        # dialog makes an enumerated instance go stale (reads 0). Sample it with short
        # typeperf runs that re-enumerate every 15 s.
        self._npu_stop = threading.Event()
        self._npu_thread = threading.Thread(target=self._npu_sampler, args=(stamp,), daemon=True)
        self._npu_thread.start()
        time.sleep(2)

    def _npu_sampler(self, stamp: str) -> None:
        k = 0
        while not self._npu_stop.is_set():
            csv = self.out / f"counters_npueng_{self.backend}_{self.tag}_{stamp}_{k:04d}.csv"
            res = subprocess.run(["typeperf", "-si", "1", "-sc", "15", "-y", "-o", str(csv), self.npu_counter],
                                 capture_output=True, text=True)
            if res.returncode != 0 or not csv.exists():
                self._npu_fail += 1
                print(f"npu sampler: typeperf rc={res.returncode}: {res.stderr.strip()[:120]}", file=sys.stderr, flush=True)
                self._npu_stop.wait(5)
            else:
                self._npu_files += 1
            k += 1

    def stop_counters(self) -> None:
        if getattr(self, "_npu_stop", None):
            self._npu_stop.set()
        if self.typeperf:
            self.typeperf.terminate()
            try:
                self.typeperf.wait(5)
            except subprocess.TimeoutExpired:
                self.typeperf.kill()
            self.typeperf = None
        subprocess.run(["taskkill", "/F", "/IM", "typeperf.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # -- logging ----------------------------------------------------------------
    def _log(self, rec: dict) -> None:
        with self._lock, self.req_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def mark(self, name: str, extra: dict | None = None) -> None:
        self._log({"backend": self.backend, "tag": self.tag, "phase": "mark", "mark": name,
                   "t_start": time.time(), "t_end": time.time(), **(extra or {})})
        print(f"  -- {name}", flush=True)

    # -- one request ----------------------------------------------------------
    def run(self, phase: str, messages: list[dict], max_tokens: int, session_id: str | None,
            meta: dict) -> dict:
        prompt_tokens = count_tokens(chatml(messages), self.gguf)
        # npurun's Rewind runtime answers a divergent prefix with an EMPTY stream
        # (Genie status -1, dialog reset) and only the retry pays the rebuild. Hex CLI
        # retries empty replies, so the harness does too and books the wasted time.
        attempts, wasted_s, t_first_attempt = 0, 0.0, time.perf_counter()
        while True:
            attempts += 1
            if self.backend == "cpu":
                r = chat_ollama(self.base, self.model, messages, max_tokens, self.num_ctx, self.temperature)
            else:
                r = chat_openai(self.base, self.model, messages, max_tokens,
                                session_id if self.backend == "npu" else None, self.temperature)
            if r.text or r.error or attempts >= 3:
                break
            wasted_s += r.t_end - r.t0
        out_tokens = count_tokens(r.text, self.gguf) if r.text else 0
        ttft = (r.t_first - r.t0) if r.t_first else None
        gen_s = (r.t_end - r.t_first) if r.t_first else None
        rec = {
            "backend": self.backend, "model": self.model, "tag": self.tag, "phase": phase,
            "session_id": session_id, "prompt_tokens": prompt_tokens, "output_tokens": out_tokens,
            "chunks": r.chunks, "ttft_s": ttft, "gen_s": gen_s, "total_s": r.t_end - r.t0,
            "decode_tps": (out_tokens - 1) / gen_s if (gen_s and out_tokens > 1) else None,
            "prefill_tps": prompt_tokens / ttft if ttft else None,
            "t_start": r.t0 + self._epoch_offset, "t_end": r.t_end + self._epoch_offset,
            "t_first_attempt": t_first_attempt + self._epoch_offset,
            "attempts": attempts, "wasted_s": round(wasted_s, 3), "total_s_user": r.t_end - t_first_attempt,
            "busy_wait_s": round(r.busy_wait_s, 3), "busy_retries": r.busy_retries,
            "finish_reason": r.finish_reason, "server": r.server, "error": r.error,
            "text_head": r.text[:120], **meta,
        }
        self._log(rec)
        rec["text"] = r.text
        flag = "ERR " + r.error[:60] if r.error else ""
        if attempts > 1:
            flag += f" (attempts={attempts} wasted={wasted_s:.1f}s)"
        if r.busy_retries:
            flag += f" (busy {r.busy_wait_s:.1f}s)"
        print(f"  [{phase}] in={prompt_tokens:5d} out={out_tokens:4d} ttft={ttft or 0:6.2f}s "
              f"dec={rec['decode_tps'] or 0:5.1f} tok/s total={rec['total_s']:6.1f}s {r.finish_reason} {flag}", flush=True)
        return rec

    # -- workloads --------------------------------------------------------------
    def hex_system_prompt(self, query: str) -> str:
        from hexcli import agent as ag
        return ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query=query)

    def story(self, seed_word: str) -> list[dict]:
        nonce = uuid.uuid4().hex
        return [{"role": "system", "content": f"Request {nonce}. You are a storyteller."},
                {"role": "user", "content": f"Write a long, detailed story about {seed_word}. Keep going "
                                            "until you are stopped; do not end the story."}]

    # -- phases -----------------------------------------------------------------
    def phase_idle(self, seconds: int) -> None:
        self.mark("idle_start", {"seconds": seconds})
        time.sleep(seconds)
        self.mark("idle_end")

    def phase_prefill(self, sizes: list[int], repeats: int, variants: str = "cold,prefixed") -> None:
        """(a) COLD: unique system prompt → no prefix reuse anywhere (on npurun this
        includes a Genie dialog rebuild). (b) APPEND on the same session: previous
        messages + reply + a short follow-up → prefix-cache hit on every backend.
        (c) PREFIXED: Hex's real pattern — the same ~2.3K-token stable system prompt
        every time, only the user content is new and N tokens long."""
        for n in (sizes if "cold" in variants else []):
            for i in range(repeats):
                nonce = uuid.uuid4().hex
                body = make_text(n - 40, self.gguf, seed=n * 100 + i)
                msgs = [{"role": "system", "content": f"Request {nonce}. You are a concise assistant."},
                        {"role": "user", "content": body + "\n\nIn one sentence, what is this text about?"}]
                sid = uuid.uuid4().hex
                r1 = self.run("prefill_cold", msgs, 32, session_id=sid, meta={"target": n, "rep": i})
                msgs2 = msgs + [{"role": "assistant", "content": r1["text"] or "It is about a system."},
                                {"role": "user", "content": "Say that again in five words."}]
                self.run("prefill_append", msgs2, 16, session_id=sid, meta={"target": n, "rep": i})
        sys_prompt = self.hex_system_prompt("summarise a file")
        # the ~2.3K-token Hex prefix plus the user content must stay inside the 4K window
        for n in (sorted({min(n, 1024) for n in sizes} | {512}) if "prefixed" in variants else []):
            for i in range(repeats):
                body = make_text(n - 30, self.gguf, seed=n * 100 + 50 + i)
                msgs = [{"role": "system", "content": sys_prompt},
                        {"role": "user", "content": "Here is a file:\n" + body + "\n\nIn one sentence, what is it about?"}]
                self.run("prefill_prefixed", msgs, 32, session_id=uuid.uuid4().hex, meta={"target": n, "rep": i})

    def phase_decode(self, repeats: int, out_tokens: int) -> None:
        for i in range(repeats):
            self.run("decode", self.story("a lighthouse keeper who finds a library under the sea"),
                     out_tokens, session_id=uuid.uuid4().hex, meta={"rep": i})

    def phase_hexconv(self, repeats: int) -> None:
        """A real Hex conversation: production system prompt + user turn, then three
        follow-up turns that each append a ~300-token tool result and a question —
        exactly how the transcript grows during an agent loop. Per-turn TTFT is the
        number a user feels on turn 1, 2, 3, 4. Repeat 0 is the first conversation
        after server start (genuinely cold); later repeats have the prefix cached."""
        q1 = "List the python files in the hexcli folder and tell me which one is largest."
        follow = ["Now count how many of them import json.",
                  "Which of those also define a class?",
                  "Summarise what you found in two sentences."]
        for i in range(repeats):
            sid = uuid.uuid4().hex
            msgs = [{"role": "system", "content": self.hex_system_prompt(q1) + f"\n\n(session {sid})"},
                    {"role": "user", "content": q1}]
            for turn in range(4):
                r = self.run(f"hex_turn{turn + 1}", msgs, 256, session_id=sid, meta={"rep": i, "turn": turn + 1})
                if turn == 3:
                    break
                tool = make_text(300, self.gguf, seed=7000 + i * 10 + turn)
                msgs = msgs + [{"role": "assistant", "content": r["text"] or "(no reply)"},
                               {"role": "user", "content": f"TOOL RESULT (list_directory):\n{tool}\n\n{follow[turn]}"}]

    def phase_sustain(self, seconds: int, out_tokens: int) -> None:
        self.mark("sustain_start", {"seconds": seconds})
        end, i = time.time() + seconds, 0
        while time.time() < end:
            self.run("sustain", self.story("an engineer who builds a clock out of river stones"),
                     out_tokens, session_id=uuid.uuid4().hex, meta={"rep": i})
            i += 1
        self.mark("sustain_end")

    def phase_contention(self, repeats: int, out_tokens: int, worker_levels: list[int]) -> None:
        py = sys.executable
        sys_prompt = self.hex_system_prompt("count files")
        # (a) inference under background CPU load; every level also gets one Hex-like request
        for w in worker_levels:
            procs = [subprocess.Popen([py, str(HERE / "load.py"), "spin", "900"]) for _ in range(w)]
            try:
                if w:
                    time.sleep(3)
                self.mark(f"bg_load_{w}_start", {"workers": w})
                for i in range(repeats):
                    self.run("decode_under_load", self.story("a cartographer mapping a city that changes every night"),
                             out_tokens, session_id=uuid.uuid4().hex, meta={"workers": w, "rep": i})
                body = make_text(300, self.gguf, seed=9000 + w)
                self.run("hexlike_under_load", [{"role": "system", "content": sys_prompt},
                                                {"role": "user", "content": "Here is a listing:\n" + body +
                                                 "\n\nHow many entries mention memory? Answer briefly."}],
                         64, session_id=uuid.uuid4().hex, meta={"workers": w, "rep": 0})
                self.mark(f"bg_load_{w}_end", {"workers": w})
            finally:
                for p in procs:
                    p.kill()
                for p in procs:
                    p.wait(10)
            if procs:
                time.sleep(3)
        # (b) foreground job while inference runs continuously
        for i in range(repeats):
            res = subprocess.run([py, str(HERE / "load.py"), "job"], capture_output=True, text=True)
            self.mark("fg_job_alone", {"job": json.loads(res.stdout), "rep": i})
        stop = threading.Event()

        def loop() -> None:
            k = 0
            while not stop.is_set():
                self.run("decode_bg_for_job", self.story("a glassblower on a comet"), out_tokens,
                         session_id=uuid.uuid4().hex, meta={"rep": k})
                k += 1

        th = threading.Thread(target=loop, daemon=True)
        th.start()
        time.sleep(4)
        for i in range(repeats):
            t0 = time.time()
            res = subprocess.run([py, str(HERE / "load.py"), "job"], capture_output=True, text=True)
            self.mark("fg_job_during_inference", {"job": json.loads(res.stdout), "rep": i, "job_t_start": t0})
        stop.set()
        th.join()


def identity() -> dict:
    def sh(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()[:400]
        except Exception as e:  # noqa: BLE001
            return f"ERR {e!r}"
    return {"npurun_version": sh(["npurun", "version"]), "ollama_version": sh(["ollama", "--version"]),
            "llamacpp_dir": str(LLAMA_TOKENIZE.parent), "python": sys.version.split()[0],
            "git_sha": sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=list(BASES), required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--tokenizer-model", default="qwen3:4b-instruct-2507-q4_K_M")
    ap.add_argument("--out", default=str(REPO / "docs" / "backend_study" / "data"))
    ap.add_argument("--tag", default="run")
    ap.add_argument("--phases", default="idle,prefill,decode,hexconv,sustain,contention")
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--idle-seconds", type=int, default=60)
    ap.add_argument("--sustain-seconds", type=int, default=240)
    ap.add_argument("--decode-tokens", type=int, default=256)
    ap.add_argument("--prefill-sizes", default="64,256,1024,2048")
    ap.add_argument("--prefill-variants", default="cold,prefixed")
    ap.add_argument("--workers", default="0,4,12")
    ap.add_argument("--no-counters", action="store_true")
    ap.add_argument("--num-thread", type=int, default=None, help="Ollama num_thread override (cpu backend).")
    args = ap.parse_args()
    global NUM_THREAD
    NUM_THREAD = args.num_thread

    model = args.model or {"npu": "qwen3-4b", "cpu": "qwen3:4b-instruct-2507-q4_K_M",
                           "llamacpp": "qwen3-4b-q4_0"}[args.backend]
    gguf = gguf_path(args.tokenizer_model)
    b = Bench(args.backend, model, Path(args.out), gguf, args.num_ctx, args.temperature, args.tag)
    print(f"backend={args.backend} model={model} base={b.base} tokenizer={gguf.name[:20]}")
    b.run("warmup", [{"role": "user", "content": f"Say OK. ({uuid.uuid4().hex})"}], 8,
          session_id=uuid.uuid4().hex, meta={})
    if not args.no_counters:
        b.start_counters()
        print(f"counters -> {b.counter_csv.name}  npu_luid={b.npu_luid}")
    try:
        b.mark("bench_start", {"phases": args.phases, "args": vars(args), "npu_luid": b.npu_luid,
                               "identity": identity()})
        for ph in args.phases.split(","):
            b.mark(f"phase_{ph}_start")
            if ph == "idle":
                b.phase_idle(args.idle_seconds)
            elif ph == "prefill":
                b.phase_prefill([int(x) for x in args.prefill_sizes.split(",")], args.repeats, args.prefill_variants)
            elif ph == "decode":
                b.phase_decode(args.repeats, args.decode_tokens)
            elif ph == "hexconv":
                b.phase_hexconv(args.repeats)
            elif ph == "sustain":
                b.phase_sustain(args.sustain_seconds, args.decode_tokens)
            elif ph == "contention":
                b.phase_contention(args.repeats, args.decode_tokens, [int(x) for x in args.workers.split(",")])
            else:
                print(f"unknown phase {ph}")
            b.mark(f"phase_{ph}_end")
        b.mark("bench_end", {"npu_sampler_failures": b._npu_fail, "npu_sampler_files": b._npu_files})
    finally:
        b.stop_counters()
    if args.backend == "npu" and not args.no_counters and b._npu_files == 0:
        print("ERROR: no NPU engine samples were written", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
