from __future__ import annotations

from collections.abc import Iterable

from terminal_app.oauth import OpenAICodexOAuthProvider
from terminal_app.oauth_anthropic import AnthropicOAuthProvider
from terminal_app.oauth_github_copilot import GitHubCopilotOAuthProvider
from terminal_app.oauth_types import OAuthProvider

_BUILTIN_PROVIDERS: tuple[OAuthProvider, ...] = tuple(
    [AnthropicOAuthProvider(), GitHubCopilotOAuthProvider(), OpenAICodexOAuthProvider()]
)
_registry: dict[str, OAuthProvider] = {provider.id: provider for provider in _BUILTIN_PROVIDERS}


def get_oauth_provider(provider_id: str) -> OAuthProvider | None:

    return _registry.get(provider_id)


def get_oauth_providers() -> tuple[OAuthProvider, ...]:

    return tuple(_registry.values())


def oauth_provider_ids() -> frozenset[str]:

    return frozenset(_registry)


def register_oauth_provider(provider: OAuthProvider) -> None:

    if not provider.id.strip():
        raise ValueError("OAuth provider id must not be empty")
    _registry[provider.id] = provider


def unregister_oauth_provider(provider_id: str) -> None:

    builtin = next(
        (provider for provider in _BUILTIN_PROVIDERS if provider.id == provider_id),
        None,
    )
    if builtin is None:
        _registry.pop(provider_id, None)
    else:
        _registry[provider_id] = builtin


def reset_oauth_providers(providers: Iterable[OAuthProvider] = _BUILTIN_PROVIDERS) -> None:

    _registry.clear()
    _registry.update((provider.id, provider) for provider in providers)
