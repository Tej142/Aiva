"""Unified async LLM Gateway over AIVA's Groq / Gemini / Mistral key pools."""

import asyncio
from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.agent.telemetry import log_agent
from core.shared.config_loader import load_secrets


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: List[LLMToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    first_token_ms: float = 0.0
    total_ms: float = 0.0
    finish_reason: str = "stop"
    used_json_fallback: bool = False


_RATE_LIMIT_MARKERS = ("429", "rate_limit", "rate limit", "quota", "resource_exhausted", "401", "invalid_api_key")
_MODEL_UNAVAILABLE_MARKERS = ("404", "model_not_found", "not_found", "no longer available")


def _is_rotatable_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(m in s for m in _RATE_LIMIT_MARKERS)


def _is_model_unavailable_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(m in s for m in _MODEL_UNAVAILABLE_MARKERS)


def _extract_json_tool_calls(text: str) -> tuple[str, List[LLMToolCall]]:
    """Parse JSON-mode fallback tool calls from text when native tool_calls aren't supported."""
    raw = (text or "").strip()
    if not raw or "{" not in raw:
        return raw, []

    candidates: List[str] = []
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    candidates.extend(fenced)
    if raw.startswith("{") and raw.endswith("}"):
        candidates.append(raw)

    for block in candidates:
        try:
            obj = json.loads(block.strip())
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        calls: List[LLMToolCall] = []
        if isinstance(obj.get("tool_calls"), list):
            for idx, tc in enumerate(obj["tool_calls"]):
                if isinstance(tc, dict) and tc.get("name"):
                    args = tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    calls.append(LLMToolCall(id=f"call_json_{idx}", name=str(tc["name"]), arguments=args if isinstance(args, dict) else {}))
        elif obj.get("tool"):
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            calls.append(LLMToolCall(id="call_json_0", name=str(obj["tool"]), arguments=args if isinstance(args, dict) else {}))
        if calls:
            return str(obj.get("final_answer") or "").strip(), calls

    return raw, []


class LLMGateway:
    """Pooled async LLM client with key rotation, failover, native tool calling, JSON fallback, and streaming."""

    TIER1_GROQ_MODELS = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b")
    TIER2_GEMINI_MODELS = ("gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite")
    TIER2_GROQ_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
    MISTRAL_MODELS = ("mistral-small-latest",)

    @classmethod
    def get_tier1_models(cls) -> List[str]:
        return list(cls.TIER1_GROQ_MODELS)

    @classmethod
    def get_tier2_models(cls) -> List[str]:
        return list(cls.TIER2_GEMINI_MODELS)

    def __init__(self, secrets: Optional[Dict[str, Any]] = None):
        cfg = secrets if secrets is not None else load_secrets()
        self.groq_keys: List[str] = list(cfg.get("groq_api_keys") or [])
        if not self.groq_keys and cfg.get("groq_api_key"):
            self.groq_keys = [cfg["groq_api_key"]]

        self.gemini_keys: List[str] = list(cfg.get("gemini_api_keys") or [])
        if not self.gemini_keys and cfg.get("gemini_api_key"):
            self.gemini_keys = [cfg["gemini_api_key"]]

        self.mistral_keys: List[str] = list(cfg.get("mistral_api_keys") or [])
        if not self.mistral_keys and cfg.get("mistral_api_key"):
            self.mistral_keys = [cfg["mistral_api_key"]]

        self._groq_idx = 0
        self._gemini_idx = 0
        self._mistral_idx = 0
        self._provider_cooldown: Dict[str, float] = {}

    def _make_groq_client(self, api_key: str):
        from groq import AsyncGroq
        return AsyncGroq(api_key=api_key, max_retries=0)

    def _make_gemini_client(self, api_key: str):
        from google import genai
        from google.genai import types
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=12000))

    def _make_mistral_client(self, api_key: str):
        from mistralai import Mistral
        return Mistral(api_key=api_key)

    async def _emit_delta(self, on_delta: Optional[Callable[[str], Awaitable[None]]], text: str) -> None:
        if on_delta and text:
            res = on_delta(text)
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                await res

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tier: int = 1,
        stream: bool = False,
        on_delta: Optional[Callable[[str], Any]] = None,
        temperature: float = 0.35,
        max_tokens: int = 800,
        timeout_s: float = 20.0,
    ) -> LLMResponse:
        """Execute a pooled chat/tool call using Tier 1 (fast Groq) or Tier 2 (Gemini/stronger)."""
        if tier >= 2:
            base_providers = ("gemini", "groq", "mistral")
        else:
            base_providers = ("groq", "gemini", "mistral")

        now_m = time.monotonic()
        with_keys = [
            p for p in base_providers
            if (p == "groq" and self.groq_keys)
            or (p == "gemini" and self.gemini_keys)
            or (p == "mistral" and self.mistral_keys)
        ]
        providers = [p for p in with_keys if now_m >= self._provider_cooldown.get(p, 0.0)]
        if not providers:
            providers = with_keys

        last_err: Optional[Exception] = None
        for prov in providers:
            try:
                if prov == "groq" and self.groq_keys:
                    models = self.TIER2_GROQ_MODELS if tier >= 2 else self.TIER1_GROQ_MODELS
                    return await asyncio.wait_for(
                        self._call_groq(
                            messages=messages,
                            tools=tools,
                            models=models,
                            tier=tier,
                            stream=stream,
                            on_delta=on_delta,
                            temperature=temperature,
                            max_tokens=max_tokens,
                        ),
                        timeout=timeout_s,
                    )
                if prov == "gemini" and self.gemini_keys:
                    return await asyncio.wait_for(
                        self._call_gemini(
                            messages=messages,
                            tools=tools,
                            stream=stream,
                            on_delta=on_delta,
                            temperature=temperature,
                        ),
                        timeout=timeout_s,
                    )
                if prov == "mistral" and self.mistral_keys:
                    return await asyncio.wait_for(
                        self._call_mistral(
                            messages=messages,
                            tools=tools,
                            stream=stream,
                            on_delta=on_delta,
                            temperature=temperature,
                        ),
                        timeout=timeout_s,
                    )
            except Exception as err:
                last_err = err
                err_low = str(err).lower()
                if any(m in err_low for m in ("429", "rate_limit", "rate limit", "quota", "resource_exhausted")):
                    self._provider_cooldown[prov] = time.monotonic() + 25.0
                log_agent("llm_failover", 0.0, "retry", provider=prov, err=type(err).__name__)
                continue

        raise RuntimeError(f"All LLM providers failed: {last_err}")

    async def _call_groq(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        models: tuple,
        tier: int = 1,
        stream: bool,
        on_delta: Optional[Callable[[str], Any]],
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        n_keys = len(self.groq_keys)
        last_err: Optional[Exception] = None

        for model in models:
            for offset in range(n_keys):
                k_idx = (self._groq_idx + offset) % n_keys
                client = self._make_groq_client(self.groq_keys[k_idx])
                try:
                    kwargs: Dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_completion_tokens": max_tokens,
                    }
                    if tier == 1 and "gpt-oss" in model:
                        kwargs["reasoning_effort"] = "low"
                    if tools:
                        kwargs["tools"] = tools
                        kwargs["tool_choice"] = "auto"

                    if stream:
                        kwargs["stream"] = True
                        stream_resp = await client.chat.completions.create(**kwargs)
                        text_parts: List[str] = []
                        tc_accum: Dict[int, Dict[str, str]] = {}
                        first_token_ms = 0.0
                        finish_reason = "stop"

                        async for chunk in stream_resp:
                            choices = getattr(chunk, "choices", None) or []
                            if not choices:
                                continue
                            ch0 = choices[0]
                            if getattr(ch0, "finish_reason", None):
                                finish_reason = ch0.finish_reason
                            delta = getattr(ch0, "delta", None)
                            if not delta:
                                continue
                            content = getattr(delta, "content", None)
                            if content:
                                if first_token_ms == 0.0:
                                    first_token_ms = (time.perf_counter() - t0) * 1000
                                text_parts.append(content)
                                await self._emit_delta(on_delta, content)
                            tcs = getattr(delta, "tool_calls", None) or []
                            for tc in tcs:
                                if first_token_ms == 0.0:
                                    first_token_ms = (time.perf_counter() - t0) * 1000
                                idx = int(getattr(tc, "index", 0) or 0)
                                slot = tc_accum.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                                if getattr(tc, "id", None):
                                    slot["id"] = tc.id
                                fn = getattr(tc, "function", None)
                                if fn:
                                    if getattr(fn, "name", None):
                                        slot["name"] += fn.name
                                    if getattr(fn, "arguments", None):
                                        slot["arguments"] += fn.arguments

                        full_text = "".join(text_parts).strip()
                        parsed_calls = self._parse_native_tool_calls(
                            [tc_accum[i] for i in sorted(tc_accum.keys())]
                        )
                    else:
                        resp = await client.chat.completions.create(**kwargs)
                        first_token_ms = (time.perf_counter() - t0) * 1000
                        ch0 = resp.choices[0]
                        msg = ch0.message
                        full_text = (getattr(msg, "content", None) or "").strip()
                        finish_reason = getattr(ch0, "finish_reason", None) or "stop"
                        parsed_calls = self._parse_native_tool_calls(getattr(msg, "tool_calls", None) or [])

                    used_json = False
                    if not parsed_calls and tools and full_text:
                        clean_txt, fb_calls = _extract_json_tool_calls(full_text)
                        if fb_calls:
                            parsed_calls = fb_calls
                            full_text = clean_txt
                            used_json = True

                    self._groq_idx = k_idx
                    return LLMResponse(
                        text=full_text,
                        tool_calls=parsed_calls,
                        provider="groq",
                        model=model,
                        first_token_ms=round(first_token_ms, 2),
                        total_ms=round((time.perf_counter() - t0) * 1000, 2),
                        finish_reason=finish_reason,
                        used_json_fallback=used_json,
                    )
                except Exception as err:
                    last_err = err
                    if _is_model_unavailable_error(err):
                        break  # Try next fallback model without burning remaining keys
                    if _is_rotatable_error(err):
                        continue  # Rotate to next key on this model
                finally:
                    close_fn = getattr(client, "close", None)
                    if callable(close_fn):
                        try:
                            closed = close_fn()
                            if asyncio.iscoroutine(closed) or hasattr(closed, "__await__"):
                                await closed
                        except Exception:
                            pass
        raise RuntimeError(f"Groq pool exhausted: {last_err}")

    async def _call_gemini(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        on_delta: Optional[Callable[[str], Any]],
        temperature: float,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        n_keys = len(self.gemini_keys)
        last_err: Optional[Exception] = None

        prompt_text = self._messages_to_prompt(messages, tools=tools)
        for offset in range(n_keys):
            k_idx = (self._gemini_idx + offset) % n_keys
            key = self.gemini_keys[k_idx]
            client = self._make_gemini_client(key)
            cfg_arg = None
            try:
                from google.genai import types
                func_decls = []
                if tools:
                    for t in tools:
                        fn = t.get("function") or {}
                        func_decls.append(
                            types.FunctionDeclaration(
                                name=fn.get("name", ""),
                                description=fn.get("description", ""),
                                parameters=fn.get("parameters"),
                            )
                        )
                cfg_kw: Dict[str, Any] = {"temperature": temperature}
                if func_decls:
                    cfg_kw["tools"] = [types.Tool(function_declarations=func_decls)]
                cfg_arg = types.GenerateContentConfig(**cfg_kw)
            except Exception:
                cfg_arg = None

            for gem_model in self.TIER2_GEMINI_MODELS:
                try:
                    def _invoke(m=gem_model):
                        if cfg_arg is not None:
                            return client.models.generate_content(
                                model=m,
                                contents=prompt_text,
                                config=cfg_arg,
                            )
                        return client.models.generate_content(
                            model=m,
                            contents=prompt_text,
                        )

                    resp = await asyncio.to_thread(_invoke)
                    first_token_ms = (time.perf_counter() - t0) * 1000
                    parsed_calls: List[LLMToolCall] = []
                    text_out = ""

                    cands = getattr(resp, "candidates", None) or []
                    if cands:
                        parts = getattr(getattr(cands[0], "content", None), "parts", None) or []
                        for idx, p in enumerate(parts):
                            fc = getattr(p, "function_call", None)
                            if fc and getattr(fc, "name", None):
                                args = dict(getattr(fc, "args", None) or {})
                                parsed_calls.append(LLMToolCall(id=f"gem_{idx}", name=fc.name, arguments=args))
                            elif getattr(p, "text", None):
                                text_out += p.text
                    if not text_out and getattr(resp, "text", None):
                        text_out = resp.text

                    text_out = (text_out or "").strip()
                    used_json = False
                    if not parsed_calls and tools and text_out:
                        clean_txt, fb_calls = _extract_json_tool_calls(text_out)
                        if fb_calls:
                            parsed_calls = fb_calls
                            text_out = clean_txt
                            used_json = True

                    if stream and text_out and not parsed_calls:
                        await self._emit_delta(on_delta, text_out)

                    self._gemini_idx = k_idx
                    return LLMResponse(
                        text=text_out,
                        tool_calls=parsed_calls,
                        provider="gemini",
                        model=gem_model,
                        first_token_ms=round(first_token_ms, 2),
                        total_ms=round((time.perf_counter() - t0) * 1000, 2),
                        finish_reason="tool_calls" if parsed_calls else "stop",
                        used_json_fallback=used_json,
                    )
                except Exception as err:
                    last_err = err
                    if _is_model_unavailable_error(err):
                        continue
                    if _is_rotatable_error(err):
                        break
        raise RuntimeError(f"Gemini pool exhausted: {last_err}")

    async def _call_mistral(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
        on_delta: Optional[Callable[[str], Any]],
        temperature: float,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        n_keys = len(self.mistral_keys)
        last_err: Optional[Exception] = None

        for offset in range(n_keys):
            k_idx = (self._mistral_idx + offset) % n_keys
            key = self.mistral_keys[k_idx]
            try:
                client = self._make_mistral_client(key)

                def _invoke():
                    kw: Dict[str, Any] = {
                        "model": self.MISTRAL_MODELS[0],
                        "messages": messages,
                        "temperature": temperature,
                    }
                    if tools:
                        kw["tools"] = tools
                        kw["tool_choice"] = "auto"
                    return client.chat.complete(**kw)

                resp = await asyncio.to_thread(_invoke)
                first_token_ms = (time.perf_counter() - t0) * 1000
                msg = resp.choices[0].message
                text_out = (getattr(msg, "content", None) or "").strip()
                parsed_calls = self._parse_native_tool_calls(getattr(msg, "tool_calls", None) or [])
                used_json = False
                if not parsed_calls and tools and text_out:
                    clean_txt, fb_calls = _extract_json_tool_calls(text_out)
                    if fb_calls:
                        parsed_calls = fb_calls
                        text_out = clean_txt
                        used_json = True

                if stream and text_out and not parsed_calls:
                    await self._emit_delta(on_delta, text_out)

                self._mistral_idx = k_idx
                return LLMResponse(
                    text=text_out,
                    tool_calls=parsed_calls,
                    provider="mistral",
                    model=self.MISTRAL_MODELS[0],
                    first_token_ms=round(first_token_ms, 2),
                    total_ms=round((time.perf_counter() - t0) * 1000, 2),
                    finish_reason="tool_calls" if parsed_calls else "stop",
                    used_json_fallback=used_json,
                )
            except Exception as err:
                last_err = err
                if _is_rotatable_error(err):
                    continue
        raise RuntimeError(f"Mistral pool exhausted: {last_err}")

    @staticmethod
    def _parse_native_tool_calls(raw_calls: List[Any]) -> List[LLMToolCall]:
        out: List[LLMToolCall] = []
        for idx, tc in enumerate(raw_calls or []):
            if isinstance(tc, dict):
                tid = tc.get("id") or f"call_{idx}"
                fn = tc.get("function") or tc
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or {}
            else:
                tid = getattr(tc, "id", None) or f"call_{idx}"
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", "") if fn else ""
                raw_args = getattr(fn, "arguments", "{}") if fn else "{}"

            if not name:
                continue
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                except Exception:
                    parsed_args = {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            out.append(LLMToolCall(id=str(tid), name=str(name), arguments=parsed_args))
        return out

    @staticmethod
    def _messages_to_prompt(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> str:
        lines = []
        for m in messages:
            role = str(m.get("role") or "user").upper()
            content = str(m.get("content") or "")
            if content:
                lines.append(f"{role}:\n{content}")
        if tools:
            tool_names = ", ".join((t.get("function") or {}).get("name", "") for t in tools)
            lines.append(
                f"SYSTEM NOTE: Available tools: [{tool_names}]. "
                f'If a tool call is needed and native function calling is unavailable, reply with JSON: {{"tool": "<name>", "arguments": {{...}}}}.'
            )
        return "\n\n".join(lines)

