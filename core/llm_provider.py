"""
LLM provider abstraction layer.

Design goal: the rest of sql-doctor should never know or care which LLM
backend is actually answering a question. This matters a lot in banking /
regulated environments where the allowed backend is dictated by IT policy
(e.g. "only Copilot / Azure OpenAI is approved for use on company devices"),
not by what the developer prefers.

Every provider implements the same narrow interface: given a grounded
prompt (schema + query + explain plan already embedded in the text), return
a raw text completion. All hallucination-guarding (see core/validator.py)
happens OUTSIDE the provider, so swapping providers can never change how
strictly we validate the answer.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass


class LLMError(RuntimeError):
    """Raised when a provider fails to produce a completion."""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str


class LLMProvider(abc.ABC):
    """Common interface every backend must implement."""

    name: str = "base"

    @abc.abstractmethod
    def complete(self, prompt: str, *, max_tokens: int = 600) -> LLMResponse:
        """Return a single completion for the given prompt."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """Cheap check used by the CLI to fail fast with a clear message."""
        return True


class OllamaProvider(LLMProvider):
    """
    Local, offline backend. Zero data leaves the machine/network — the
    selling point for compliance-sensitive clients (PCI-DSS, banking).
    Requires `ollama` running locally (default http://localhost:11434).
    """

    name = "ollama"

    def __init__(self, model: str = "sqlcoder", host: str | None = None):
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def is_available(self) -> bool:
        try:
            import urllib.request

            urllib.request.urlopen(f"{self.host}/api/tags", timeout=2)
            return True
        except Exception:
            return False

    def complete(self, prompt: str, *, max_tokens: int = 600) -> LLMResponse:
        import json
        import urllib.request

        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.1},
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Ollama request failed: {exc}") from exc

        return LLMResponse(text=data.get("response", ""), provider=self.name, model=self.model)


class ClaudeProvider(LLMProvider):
    """Anthropic API backend. Good default for personal / non-regulated use."""

    name = "claude"

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def complete(self, prompt: str, *, max_tokens: int = 600) -> LLMResponse:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")

        import json
        import urllib.request

        payload = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Claude API request failed: {exc}") from exc

        text = "".join(block.get("text", "") for block in data.get("content", []))
        return LLMResponse(text=text, provider=self.name, model=self.model)


class AzureOpenAIProvider(LLMProvider):
    """
    Azure OpenAI Service backend — the option relevant to banks that only
    approve Copilot / Azure-hosted models on corporate devices (ProCredit,
    DSK-style IT policy). Uses the customer's own Azure tenant, so the
    compliance story is "your data stays inside your Azure subscription",
    not "trust a third-party API".
    """

    name = "azure-openai"

    def __init__(
        self,
        deployment: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str = "2024-10-21",
    ):
        self.deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        self.endpoint = (endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        self.api_version = api_version

    def is_available(self) -> bool:
        return bool(self.deployment and self.endpoint and self.api_key)

    def complete(self, prompt: str, *, max_tokens: int = 600) -> LLMResponse:
        if not self.is_available():
            raise LLMError(
                "Azure OpenAI is not configured. Set AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_DEPLOYMENT and AZURE_OPENAI_API_KEY."
            )

        import json
        import urllib.request

        url = (
            f"{self.endpoint}/openai/deployments/{self.deployment}"
            f"/chat/completions?api-version={self.api_version}"
        )
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "api-key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Azure OpenAI request failed: {exc}") from exc

        text = data["choices"][0]["message"]["content"]
        return LLMResponse(text=text, provider=self.name, model=self.deployment or "")


_PROVIDERS = {
    "ollama": OllamaProvider,
    "claude": ClaudeProvider,
    "azure-openai": AzureOpenAIProvider,
}


def get_provider(name: str, **kwargs) -> LLMProvider:
    """Factory used by the CLI (--llm-provider flag)."""
    try:
        cls = _PROVIDERS[name]
    except KeyError as exc:
        valid = ", ".join(_PROVIDERS)
        raise ValueError(f"Unknown LLM provider '{name}'. Valid options: {valid}") from exc
    return cls(**kwargs)
