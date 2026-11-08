import asyncio
import json
import unittest
from unittest.mock import patch

from backend.config import _normalize_provider
from backend.proxy import (
    _compact_request_body,
    _redact_upstream_error_body,
    _request_size_metrics,
    _tokens_per_minute_hint,
    forward_request,
    forward_request_stream,
    log_buffer,
)


class RequestMetricsAndCompactionTests(unittest.TestCase):
    def test_redacts_provider_api_key_and_bearer_token_and_truncates(self):
        provider = {"apiKey": "gsk_live_provider_secret_123456"}
        raw = (
            'upstream said api_key="gsk_live_provider_secret_123456"; '
            "Authorization: Bearer long-bearer-token-1234567890 "
            "Authorization: Bearer short "
            + "x" * 800
        )

        redacted = _redact_upstream_error_body(raw, provider)

        self.assertLessEqual(len(redacted), 500)
        self.assertNotIn(provider["apiKey"], redacted)
        self.assertNotIn("long-bearer-token-1234567890", redacted)
        self.assertNotIn("Bearer short", redacted)
        self.assertIn("******", redacted)

    def test_request_size_metrics_count_utf8_bytes_and_prompt_shape(self):
        body = {
            "model": "model-x",
            "system": [{"type": "text", "text": "\u4f60\u597d"}, {"type": "text", "text": "System"}],
            "tools": [{"name": "read_file"}, {"name": "search"}],
            "messages": [{"role": "user", "content": "hello"}],
        }
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        metrics = _request_size_metrics(body, raw)

        self.assertEqual(metrics["bytes"], len(raw))
        self.assertEqual(metrics["characters"], len(raw.decode("utf-8")))
        self.assertEqual(metrics["estimated_tokens"], metrics["characters"] // 4)
        self.assertEqual(metrics["system_characters"], len("\u4f60\u597d\nSystem"))
        self.assertEqual(metrics["tool_count"], 2)
        self.assertEqual(metrics["message_count"], 1)

    def test_compaction_is_a_noop_when_disabled(self):
        body = {
            "system": "Keep me as-is",
            "tools": [{"name": "read_file"}],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": "hello"}],
        }

        compacted = _compact_request_body(body, {"compactRequests": False})

        self.assertIs(compacted, body)
        self.assertIn("tools", compacted)
        self.assertIn("tool_choice", compacted)
        self.assertEqual(compacted["system"], "Keep me as-is")

    def test_compaction_drops_tools_caps_prompt_and_keeps_latest_messages(self):
        system = "BEGIN " + ("middle " * 50) + "END"
        body = {
            "system": system,
            "tools": [{"name": "read_file"}],
            "tool_choice": {"type": "any"},
            "messages": [{"role": "user", "content": f"message-{i}"} for i in range(5)],
        }

        compacted = _compact_request_body(body, {
            "compactRequests": True,
            "compactSystemPromptLimit": 24,
            "compactMessageLimit": 2,
        })

        self.assertNotIn("tools", compacted)
        self.assertNotIn("tool_choice", compacted)
        self.assertLessEqual(len(compacted["system"]), 24)
        self.assertTrue(compacted["system"].startswith("BEGIN"))
        self.assertTrue(compacted["system"].endswith("END"))
        self.assertEqual([m["content"] for m in compacted["messages"]], ["message-3", "message-4"])
        self.assertEqual(len(body["messages"]), 5)
        self.assertIn("tools", body)

    def test_compaction_reaches_openai_provider_payload(self):
        captured = {}

        class FakeElapsed:
            def total_seconds(self):
                return 0.01

        class FakeResponse:
            status_code = 200
            is_success = True
            headers = {"content-type": "application/json"}
            elapsed = FakeElapsed()

            def json(self):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                captured.update(kwargs)
                return FakeResponse()

        body = {
            "model": "model-x",
            "system": "BEGIN " + ("x" * 100) + " END",
            "messages": [{"role": "user", "content": f"message-{i}"} for i in range(5)],
            "tools": [{"name": "read_file"}],
            "tool_choice": {"type": "auto"},
            "max_tokens": 8,
        }
        provider = {
            "baseUrl": "https://api.provider.invalid/v1",
            "apiKey": "provider-secret",
            "apiFormat": "openai_chat",
            "compactRequests": True,
            "compactSystemPromptLimit": 20,
            "compactMessageLimit": 2,
        }

        with patch("backend.proxy.httpx.AsyncClient", FakeClient):
            result = asyncio.run(forward_request(body, provider, "req-compact"))

        self.assertNotIn("error", result)
        upstream = captured["json"]
        self.assertNotIn("tools", upstream)
        self.assertNotIn("tool_choice", upstream)
        self.assertTrue(upstream["messages"][0]["content"].startswith("BEGIN"))
        self.assertTrue(upstream["messages"][0]["content"].endswith("END"))
        self.assertEqual(len(upstream["messages"][0]["content"]), 20)
        self.assertEqual([message["content"] for message in upstream["messages"][1:]], ["message-3", "message-4"])

    def test_provider_config_defaults_compaction_to_off_and_bounds_limits(self):
        defaulted = _normalize_provider({"id": "p1"})
        bounded = _normalize_provider({
            "id": "p2",
            "compactRequests": True,
            "compactSystemPromptLimit": 999999,
            "compactMessageLimit": 0,
        })

        self.assertFalse(defaulted["compactRequests"])
        self.assertEqual(defaulted["compactSystemPromptLimit"], 4000)
        self.assertEqual(defaulted["compactMessageLimit"], 12)
        self.assertTrue(bounded["compactRequests"])
        self.assertEqual(bounded["compactSystemPromptLimit"], 50000)
        self.assertEqual(bounded["compactMessageLimit"], 1)

    def test_413_tokens_per_minute_hint_includes_available_numbers(self):
        hint = _tokens_per_minute_hint(
            413,
            "Rate limit reached on tokens per minute (TPM): Limit 6,000, Requested 9,250",
        )

        self.assertIn("per-minute token limit was exceeded", hint)
        self.assertIn("6,000", hint)
        self.assertIn("9,250", hint)
        self.assertIn("Compact requests", hint)
        self.assertIsNone(_tokens_per_minute_hint(413, "request too large"))
        self.assertIsNone(_tokens_per_minute_hint(429, "tokens per minute: Limit 10"))


class UpstreamErrorLoggingTests(unittest.TestCase):
    def setUp(self):
        log_buffer.clear()

    def test_non_streaming_413_logs_redacted_body_and_returns_friendly_hint(self):
        secret = "gsk_live_provider_secret_123456"
        error_body = (
            "Rate limit reached on tokens per minute (TPM): Limit 6,000, Requested 9,250; "
            f"api_key={secret} " + "x" * 700
        )

        class FakeElapsed:
            def total_seconds(self):
                return 0.01

        class FakeResponse:
            status_code = 413
            is_success = False
            headers = {"content-type": "application/json"}
            text = error_body
            elapsed = FakeElapsed()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        provider = {
            "baseUrl": "https://api.provider.invalid/v1",
            "apiKey": secret,
            "apiFormat": "anthropic",
        }
        body = {"model": "model-x", "messages": [{"role": "user", "content": "hello"}]}

        with patch("backend.proxy.httpx.AsyncClient", FakeClient):
            result = asyncio.run(forward_request(body, provider, "req-413"))

        self.assertIn("per-minute token limit was exceeded", result["error"]["message"])
        self.assertIn("6,000", result["error"]["message"])
        messages = "\n".join(item["message"] for item in log_buffer.get_all())
        self.assertIn("error body", messages.lower())
        self.assertIn("Compact requests", messages)
        self.assertNotIn(secret, messages)
        logged_body = next(item["message"] for item in log_buffer.get_all() if "error body" in item["message"].lower())
        self.assertLessEqual(len(logged_body.split(": ", 1)[-1]), 500)

    def test_streaming_413_logs_redacted_body_and_friendly_error_event(self):
        secret = "gsk_live_provider_secret_123456"
        raw = f"tokens per minute TPM Limit 6000 Requested 9200 api_key={secret}".encode()

        class FakeStreamResponse:
            status_code = 413
            is_success = False
            headers = {"content-type": "application/json"}

            async def aread(self):
                return raw

        class FakeStreamContext:
            async def __aenter__(self):
                return FakeStreamResponse()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def stream(self, *args, **kwargs):
                return FakeStreamContext()

        provider = {
            "baseUrl": "https://api.provider.invalid/v1",
            "apiKey": secret,
            "apiFormat": "anthropic",
        }
        body = {"model": "model-x", "messages": [{"role": "user", "content": "hello"}], "stream": True}

        async def collect():
            return "".join([chunk async for chunk in forward_request_stream(body, provider, "req-stream-413")])

        with patch("backend.proxy.httpx.AsyncClient", FakeClient):
            event_text = asyncio.run(collect())

        self.assertIn("event: error", event_text)
        self.assertIn("per-minute token limit was exceeded", event_text)
        self.assertIn("6000", event_text)
        messages = "\n".join(item["message"] for item in log_buffer.get_all())
        self.assertIn("error body", messages.lower())
        self.assertIn("Compact requests", messages)
        self.assertNotIn(secret, messages)


if __name__ == "__main__":
    unittest.main()
