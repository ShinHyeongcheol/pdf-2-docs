"""Deferred Gemini Developer API adapter with native JSON schema and bounded requests."""
import json
import os
import re
from collections.abc import Callable
from threading import Lock

import httpx

from .openai_adapter import request_messages
from .quiz import GenerationBlocked, LessonContext, QuizBatch, QuizPolicy


class GeminiQuizAdapter:
    mode = "gemini"

    def __init__(self, *, client_factory: Callable | None = None, key_provider: Callable[[], str | None] | None = None,
                 request_validator: Callable[[httpx.Request], None] | None = None):
        self._client_factory = client_factory
        self._key_provider = key_provider
        self._request_validator = request_validator
        self._calls = 0
        self._budget_lock = Lock()

    def generate(self, context: LessonContext, policy: QuizPolicy, feedback: list[str]) -> QuizBatch:
        policy = QuizPolicy.model_validate(policy.model_dump())
        if not policy.allow_network or not policy.budget_confirmed or not policy.capabilities_confirmed:
            raise GenerationBlocked("network, budget and model capabilities require explicit approval")
        if not policy.model.strip():
            policy.model = os.environ.get("PDF_NOTION_GEMINI_MODEL", "")
        if not re.fullmatch(r"gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,120}", policy.model):
            raise GenerationBlocked("explicit Gemini model ID required")
        if not policy.max_calls or not policy.max_request_bytes or not policy.max_output_tokens:
            raise GenerationBlocked("positive request/output/call limits required")
        if self._calls >= policy.max_calls:
            raise GenerationBlocked("call budget exhausted")
        messages = request_messages(context, policy, feedback)
        size = len(json.dumps({"messages": [m.content for m in messages], "schema": QuizBatch.model_json_schema()}, ensure_ascii=False).encode())
        if size > policy.max_request_bytes:
            raise GenerationBlocked("request byte limit exceeded")
        key = self._key_provider() if self._key_provider is not None else os.environ.get("GEMINI_API_KEY")
        if not key or not key.strip():
            raise GenerationBlocked("GEMINI_API_KEY is not configured")
        factory = self._client_factory
        if factory is None:
            from langchain_google_genai import ChatGoogleGenerativeAI
            factory = ChatGoogleGenerativeAI
        with self._budget_lock:
            if self._calls >= policy.max_calls:
                raise GenerationBlocked("call budget exhausted")
            self._calls += 1
        requests = 0

        def guard_request(request: httpx.Request):
            nonlocal requests
            expected = "/v1beta/models/" + policy.model + ":generateContent"
            if (request.method != "POST" or request.url.scheme != "https" or
                request.url.host != "generativelanguage.googleapis.com" or
                request.url.port not in (None, 443) or request.url.path != expected or request.url.query):
                raise GenerationBlocked("only the explicit Gemini generation endpoint is allowed")
            if len(request.content) > policy.max_request_bytes or requests:
                raise GenerationBlocked("serialized byte limit or SDK retry exceeded")
            if self._request_validator is not None:
                self._request_validator(request)
            requests += 1

        client = factory(model=policy.model, api_key=key, vertexai=False,
            base_url="https://generativelanguage.googleapis.com", api_version="v1beta",
            timeout=policy.timeout_seconds, max_retries=0,
            max_output_tokens=policy.max_output_tokens,
            client_args={"trust_env": False, "event_hooks": {"request": [guard_request]}})
        try:
            structured = client.with_structured_output(QuizBatch, method="json_schema")
            return structured.invoke(messages)
        finally:
            # Google SDK owns its clients; close after the single synchronous invocation.
            sdk = getattr(client, "client", None)
            if sdk is not None:
                sdk.close()
