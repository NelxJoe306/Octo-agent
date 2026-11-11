from __future__ import annotations

from urllib.parse import urlsplit

OPENAI_API_HOST = "api.openai.com"
OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH = 64


def openai_prompt_cache_key(session_id: str | None) -> str | None:

    if not session_id:
        return None
    return session_id[:OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH]


def is_direct_openai_url(base_url: str) -> bool:

    return urlsplit(base_url).hostname == OPENAI_API_HOST
