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
from pathlib import Path


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


class ManualProvider(LLMProvider):
    """
    For the common bank IT scenario: employees have an M365 Copilot
    *chat* license, but no programmatic API access (that would require
    an Entra ID app registration, admin approval, and is still preview).

    Instead of calling an API, this provider writes the grounded prompt
    to a temp file AND prints it, so the user can paste it into whatever
    sanctioned AI chat tool they already have access to (Copilot,
    ChatGPT Enterprise, etc.), then paste the response into a response
    file and press Enter to continue.

    Why a file instead of reading multi-line paste directly from stdin:
    pasting multi-line text into a running console app's input() loop is
    unreliable on Windows terminals (PowerShell/cmd) — the paste can
    arrive as a premature EOF, or the app may have already exited by the
    time the remaining lines arrive, which then get interpreted as
    separate shell commands. Found via real testing. A file sidesteps
    this entirely: no interactive multi-line stdin parsing needed.
    """

    name = "manual"

    def __init__(self, response_file: str | None = None, quick: bool = False):
        import tempfile

        self.quick = quick
        self.response_file = response_file or str(
            Path(tempfile.gettempdir()) / "sql_doctor_response.txt"
        )

    def is_available(self) -> bool:
        return True  # always "available" — no credentials needed

    def complete(self, prompt: str, *, max_tokens: int = 600) -> LLMResponse:
        print("\n" + "=" * 70)
        print("COPY the prompt below into your Copilot / chat tool of choice:")
        print("=" * 70)
        print(prompt)
        print("=" * 70)

        if self.quick:
            print(
                "\n(--quick mode: exiting without waiting for a response. "
                "Read the AI's answer yourself — no automated validation "
                "will run.)"
            )
            return LLMResponse(text="", provider=self.name, model="manual-quick")

        # Clear any stale content from a previous run.
        Path(self.response_file).write_text("", encoding="utf-8")
        print(f"\nPaste the AI's FULL response into this file, then SAVE it:")
        print(f"  {self.response_file}")
        input("\nOnce saved, press Enter here to continue...")

        text = Path(self.response_file).read_text(encoding="utf-8").strip()
        if not text:
            print(
                f"Warning: {self.response_file} was empty — did you save it "
                "before pressing Enter?"
            )

        return LLMResponse(text=text, provider=self.name, model="manual-paste")


_PROVIDERS = {
    "ollama": OllamaProvider,
    "claude": ClaudeProvider,
    "azure-openai": AzureOpenAIProvider,
    "manual": ManualProvider,
}


def get_provider(name: str, **kwargs) -> LLMProvider:
    """Factory used by the CLI (--llm-provider flag)."""
    try:
        cls = _PROVIDERS[name]
    except KeyError as exc:
        valid = ", ".join(_PROVIDERS)
        raise ValueError(f"Unknown LLM provider '{name}'. Valid options: {valid}") from exc
    return cls(**kwargs)
