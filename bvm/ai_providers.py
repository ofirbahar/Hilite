"""AI assistants the monitor can ask. Official APIs only, keys come from the environment.

Model names change often, so none is hard-wired for OpenAI or Perplexity:
set the key AND the model name, and that assistant is switched on.

    ANTHROPIC_API_KEY   (+ BVM_ANTHROPIC_MODEL, optional)
                        (+ BVM_ANTHROPIC_WEB_SEARCH=1: Claude searches the web before it
                         answers, as it does in its own app, and names the sites it used.
                         Anthropic bills every search on top of the answer.)
    OPENAI_API_KEY      + BVM_OPENAI_MODEL
    PERPLEXITY_API_KEY  + BVM_PERPLEXITY_MODEL
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests

TIMEOUT = 90
SEARCH_TIMEOUT = 180       # an answer that searches the web first takes longer
# Room for the whole answer. The model's own reasoning counts against this too, and an
# answer cut off in the middle of a list would miss the businesses named at its end.
MAX_TOKENS = 4000
MAX_SEARCHES = 3           # web searches Claude may make for one question
MAX_RESUMES = 3            # times a long search turn may be picked up where it paused
WEB_SEARCH = "web_search_20260209"


@dataclass
class Answer:
    text: str
    citations: list[str] = field(default_factory=list)


def _system(country: str) -> str:
    return f"The person asking is a buyer located in {country}. Answer as you normally would."


class AnthropicProvider:
    def __init__(self, key: str, model: str = "claude-sonnet-5-5",
                 base_url: str = "https://api.anthropic.com", name: str = "claude",
                 web_search: bool = False):
        """web_search: let Claude search the web before answering. The answer is then closer
        to what a person sees in Claude's own app, and comes with the sites it relied on."""
        self.key, self.model, self.base_url, self.name = key, model, base_url.rstrip("/"), name
        self.web_search = web_search

    def _search_tool(self, country: str) -> dict:
        tool = {"type": WEB_SEARCH, "name": "web_search", "max_uses": MAX_SEARCHES}
        if len(country) == 2 and country.isalpha():      # results as a buyer there gets them
            tool["user_location"] = {"type": "approximate", "country": country.upper()}
        return tool

    def ask(self, question: str, country: str) -> Answer:
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "system": _system(country),
                "messages": [{"role": "user", "content": question}]}
        if self.web_search:
            body["tools"] = [self._search_tool(country)]
        text, cited = "", []
        for _ in range(MAX_RESUMES + 1):
            response = requests.post(
                f"{self.base_url}/v1/messages",
                headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body, timeout=SEARCH_TIMEOUT if self.web_search else TIMEOUT,
            )
            response.raise_for_status()
            reply = response.json()
            blocks = reply.get("content") or []
            for block in blocks:
                if block.get("type") != "text":
                    continue                 # searches and their raw results are not the answer
                text += block.get("text", "")
                # The sites this sentence rests on. What a search merely returned and the
                # answer did not use is left out: it is not a site the AI relied on.
                cited += [c["url"] for c in block.get("citations") or []
                          if isinstance(c, dict) and isinstance(c.get("url"), str)]
            if reply.get("stop_reason") != "pause_turn":
                break
            # A long search turn stops part-way. Sending it back as it came picks it up.
            body["messages"] = [*body["messages"], {"role": "assistant", "content": blocks}]
        return Answer(text, list(dict.fromkeys(cited)))


class ChatCompletionsProvider:
    """Any assistant that speaks the OpenAI chat-completions format."""

    def __init__(self, name: str, key: str, model: str, base_url: str):
        self.name, self.key, self.model, self.base_url = name, key, model, base_url.rstrip("/")

    def ask(self, question: str, country: str) -> Answer:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            json={"model": self.model,
                  "messages": [{"role": "system", "content": _system(country)},
                               {"role": "user", "content": question}]},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"].get("content") or ""
        citations = [c for c in body.get("citations") or [] if isinstance(c, str)]
        return Answer(text, citations)


def providers_from_env(env: dict | None = None) -> list:
    env = os.environ if env is None else env
    providers: list = []
    if env.get("ANTHROPIC_API_KEY"):
        providers.append(AnthropicProvider(
            env["ANTHROPIC_API_KEY"], env.get("BVM_ANTHROPIC_MODEL") or "claude-sonnet-5-5",
            web_search=(env.get("BVM_ANTHROPIC_WEB_SEARCH") or "").strip().lower()
            in ("1", "true", "yes", "on")))
    if env.get("OPENAI_API_KEY") and env.get("BVM_OPENAI_MODEL"):
        providers.append(ChatCompletionsProvider(
            "chatgpt", env["OPENAI_API_KEY"], env["BVM_OPENAI_MODEL"],
            env.get("BVM_OPENAI_URL") or "https://api.openai.com/v1"))
    if env.get("PERPLEXITY_API_KEY") and env.get("BVM_PERPLEXITY_MODEL"):
        providers.append(ChatCompletionsProvider(
            "perplexity", env["PERPLEXITY_API_KEY"], env["BVM_PERPLEXITY_MODEL"],
            env.get("BVM_PERPLEXITY_URL") or "https://api.perplexity.ai"))
    return providers
