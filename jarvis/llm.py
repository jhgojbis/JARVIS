"""LLM backends. All return plain text; callers ask for JSON when they need structure.

 - anthropic : Anthropic API (Haiku by default, cheapest)
 - claude_cli: shells out to `claude -p` so it runs on the user's own Claude Pro/Max login
 - A daily token budget is enforced for every backend.
"""
from __future__ import annotations
import json, re, subprocess
from .config import Config
from .store import Store


class BudgetExceeded(Exception):
    pass


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
        r = subprocess.run(["claude", "-p", "--model", "haiku", "--append-system-prompt", system, convo],
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {r.stderr[:200]}")
        return r.stdout.strip()

    def ask_json(self, system: str, messages, max_tokens: int | None = None) -> dict:
        """Ask for a JSON object; tolerant of code fences / prose around it."""
        text = self.ask(system + "\nRespond with a single JSON object only, no prose.", messages, max_tokens)
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            return {}
