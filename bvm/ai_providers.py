"""AI assistants the monitor can ask. Official APIs only, keys come from the environment.

Model names change often, so none is hard-wired for OpenAI or Perplexity:
set the key AND the model name, and that assistant is switched on.

    ANTHROPIC_API_KEY   (+ BVM_ANTHROPIC_MODEL, optional)
    OPENAI_API_KEY      + BVM_OPENAI_MODEL
    PERPLEXITY_API_KEY  + BVM_PERPLEXITY_MODEL
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import requests

TIMEOUT = 90
MAX_TOKENS = 900


@dataclass
class Answer:
    text: str
    citations: list[str] = field(default_factory=list)


def _system(country: str) -> str:
    return f"The person asking is a buyer located in {country}. Answer as you normally would."


class AnthropicProvider:
    def __init__(self, key: str, model: str = "claude-sonnet-5-5",
                 base_url: str = "https://api.anthropic.com", name: str = "claude"):
        self.key, self.model, self.base_url, self.name = key, model, base_url.rstrip("/"), name

    def ask(self, question: str, country: str) -> Answer:
        response = requests.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": MAX_TOKENS, "system": _system(country),
                  "messages": [{"role": "user", "content": question}]},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        return Answer("".join(b.get("text", "") for b in blocks if b.get("type") == "text"))


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
            env["ANTHROPIC_API_KEY"], env.get("BVM_ANTHROPIC_MODEL") or "claude-sonnet-5-5"))
    if env.get("OPENAI_API_KEY") and env.get("BVM_OPENAI_MODEL"):
        providers.append(ChatCompletionsProvider(
            "chatgpt", env["OPENAI_API_KEY"], env["BVM_OPENAI_MODEL"],
            env.get("BVM_OPENAI_URL") or "https://api.openai.com/v1"))
    if env.get("PERPLEXITY_API_KEY") and env.get("BVM_PERPLEXITY_MODEL"):
        providers.append(ChatCompletionsProvider(
            "perplexity", env["PERPLEXITY_API_KEY"], env["BVM_PERPLEXITY_MODEL"],
            env.get("BVM_PERPLEXITY_URL") or "https://api.perplexity.ai"))
    return providers
