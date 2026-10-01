"""Explicit provider selection; no fallback, arbitrary endpoint or eager key loading."""
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .quiz import GenerationBlocked, LessonContext, QuizBatch, QuizGenerator, QuizPolicy

KEY_NAMES = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}


class ProviderSettings(Contract):
    provider: Literal["gemini", "openai"] = "gemini"
    model: str = ""
    # User-reviewed text + JSON-schema capability allowlist; no guessed default models.
    approved_models: list[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None):
        values = os.environ if values is None else values
        provider = values.get("PDF_NOTION_PROVIDER", "gemini")
        model = values.get("PDF_NOTION_MODEL", "")
        approved = [s.strip() for s in values.get("PDF_NOTION_APPROVED_MODELS", "").split(",") if s.strip()]
        try:
            return cls(provider=provider, model=model, approved_models=approved)
        except ValueError:
            # Do not include raw configuration values in diagnostic text.
            raise GenerationBlocked("unsupported provider configuration") from None


def selected_key(name: str, project_root: Path | None = None) -> str | None:
    """Called only by a gated adapter. Optional local .env is data, never shell code."""
    if name not in KEY_NAMES.values():
        raise GenerationBlocked("unsupported key name")
    value = os.environ.get(name)
    if value and value.strip():
        return value
    if project_root is None:
        return None
    path = project_root / ".env"
    if path.is_symlink():
        raise GenerationBlocked("dotenv symlinks are not supported")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 16_384:
        raise GenerationBlocked("invalid dotenv file")
    # Only select this provider's key. Ignore unrelated assignments; never populate os.environ.
    matches = []
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*", line)
        if match:
            value = match.group(1)
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise GenerationBlocked("malformed selected dotenv key")
                value = value[1:-1]
            if not value.strip() or any(c in value for c in "$`\\\n\r"):
                raise GenerationBlocked("blank or interpolated dotenv key is unsupported")
            matches.append(value)
    if len(matches) > 1:
        raise GenerationBlocked("duplicate selected dotenv key")
    return matches[0] if matches else None


class ConfiguredQuizAdapter:
    def __init__(self, settings: ProviderSettings, delegate: QuizGenerator):
        self._settings_json = ProviderSettings.model_validate(settings.model_dump()).model_dump_json()
        self.delegate = delegate
        self.mode = settings.provider

    def generate(self, context: LessonContext, policy: QuizPolicy, feedback: list[str]) -> QuizBatch:
        settings = ProviderSettings.model_validate_json(self._settings_json)
        policy = QuizPolicy.model_validate(policy.model_dump())
        if not policy.allow_network or not policy.budget_confirmed or not policy.capabilities_confirmed:
            raise GenerationBlocked("explicit approvals required")
        if (not settings.model or settings.model not in settings.approved_models or
                (policy.model and policy.model != settings.model)):
            raise GenerationBlocked("selected model requires explicit capability approval")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", settings.model):
            raise GenerationBlocked("invalid model ID")
        if settings.provider == "gemini" and not settings.model.startswith("gemini-"):
            raise GenerationBlocked("unsupported Gemini model")
        if settings.provider == "openai" and settings.model.startswith("gemini-"):
            raise GenerationBlocked("unsupported OpenAI model")
        if self.delegate.mode != settings.provider:
            raise GenerationBlocked("provider adapter mismatch")
        policy.model = settings.model
        return self.delegate.generate(context.model_copy(deep=True), policy, list(feedback))


def build_provider(settings: ProviderSettings | None = None, *, project_root: Path | None = None,
                   client_factory: Callable | None = None, key_provider: Callable[[], str | None] | None = None,
                   request_validator: Callable | None = None) -> ConfiguredQuizAdapter:
    settings = ProviderSettings.model_validate((settings or ProviderSettings.from_env()).model_dump())
    provider = settings.provider
    loader = key_provider if key_provider is not None else lambda: selected_key(KEY_NAMES[provider], project_root)
    if provider == "gemini":
        from .gemini_adapter import GeminiQuizAdapter
        delegate = GeminiQuizAdapter(client_factory=client_factory, key_provider=loader, request_validator=request_validator)
    else:
        from .openai_adapter import OpenAIQuizAdapter
        delegate = OpenAIQuizAdapter(client_factory=client_factory, key_provider=loader, request_validator=request_validator)
    return ConfiguredQuizAdapter(settings, delegate)
