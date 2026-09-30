"""LLM backends. All return plain text; callers ask for JSON when they need structure.

 - anthropic : Anthropic API (Haiku by default, cheapest)
 - claude_cli: shells out to `claude -p` so it runs on the user's own Claude Pro/Max login
 - A daily token budget is enforced for every backend.
"""
from __future__ import annotations
import atexit, json, os, re, subprocess, threading
from .config import Config
from .store import Store


class BudgetExceeded(Exception):
    pass


class WarmClaude:
    """`claude -p` takes seconds to start, too slow for a phone call. Keep one process started ahead of time and
    use each process for a single request (so nothing leaks between requests); the next one starts in the background.
    MCP servers, settings/hooks, skills and tools are skipped; the real instructions travel inside the message."""
    ARGS = ["claude", "-p", "--model", "haiku", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--system-prompt", "Follow the <instructions> in the user's message exactly.",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--disable-slash-commands", "--no-session-persistence", "--tools", ""]

    def __init__(self):
        self._lock, self._next = threading.Lock(), None
        atexit.register(lambda: self._next and self._next.kill())

    def _spawn(self) -> subprocess.Popen:
        # Extended thinking roughly triples reply time; a phone call needs speed more than deliberation.
        return subprocess.Popen(self.ARGS, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1, env={**os.environ, "MAX_THINKING_TOKENS": "0"})

    def ask(self, prompt: str, timeout: float = 90) -> str:
        with self._lock:
            p = self._next if self._next and self._next.poll() is None else self._spawn()
            self._next = self._spawn()
        killer = threading.Timer(timeout, p.kill)
        killer.start()
        try:
            p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            p.stdin.flush()
            for line in p.stdout:
                ev = json.loads(line)
                if ev.get("type") == "result":
                    if ev.get("is_error"):
                        raise RuntimeError(f"claude CLI failed: {str(ev.get('result'))[:200]}")
                    return str(ev.get("result") or "").strip()
            raise RuntimeError("claude CLI exited without a result")
        finally:
            killer.cancel()
            p.kill()


_warm = WarmClaude()


class LLM:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self._client = None

    def ask(self, system: str, messages: list[dict] | str, max_tokens: int | None = None) -> str:
        c = self.cfg["llm"]
        if self.store.tokens_today() >= c["daily_token_budget"]:
            raise BudgetExceeded("daily token budget reached")
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if c["backend"] == "claude_cli":
            out = self._cli(system, messages)
            self.store.add_tokens((len(system) + sum(len(m["content"]) for m in messages) + len(out)) // 4)
            return out
        return self._api(system, messages, max_tokens or c["max_tokens"])

    def _api(self, system, messages, max_tokens):
        import anthropic
        if self._client is None:
            self._client = anthropic.Anthropic(max_retries=2, timeout=20)
        r = self._client.messages.create(
            model=self.cfg["llm"]["model"], max_tokens=max_tokens,
            # cache the (static) system prompt; cheap repeat calls
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages)
        self.store.add_tokens(r.usage.input_tokens + r.usage.output_tokens)
        return "".join(b.text for b in r.content if b.type == "text")

    def _cli(self, system, messages):
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        return _warm.ask(f"<instructions>\n{system}\n</instructions>\n\n{convo}")

    def ask_json(self, system: str, messages, max_tokens: int | None = None) -> dict:
        """Ask for a JSON object; tolerant of code fences / prose around it."""
        text = self.ask(system + "\nRespond with a single JSON object only, no prose.", messages, max_tokens)
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            return {}
