# Junction v1.0.30

This release adds opt-in request compaction and clearer diagnostics for providers with small request or tokens-per-minute limits.

- Added per-provider Compact requests, off by default, with configurable system-prompt and recent-message limits.
- Compact mode removes tool definitions and `tool_choice`, keeps the beginning and end of long system prompts, and warns that tools such as file access, web search, and artifacts will not work.
- Added request-size and prompt-shape metrics, including body bytes, estimated input tokens, system-prompt characters, tool count, and message count.
- Logged redacted upstream error bodies for streaming and non-streaming failures, truncated to 500 characters.
- Added a clear explanation for 413 tokens-per-minute limit errors, including limit/requested figures when available and suggestions to compact requests or use a higher-limit model.

Compact requests remains disabled by default, so existing provider request behavior is unchanged unless enabled per provider.
