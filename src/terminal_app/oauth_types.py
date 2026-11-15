from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_core.types import JSONValue
from terminal_app.credentials import OAuthCredential

OAuthFlowKind = Literal["browser", "device_code"]


@dataclass(frozen=True, slots=True)
class OAuthAuthInfo:


    url: str
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthDeviceCodeInfo:


    user_code: str
    verification_uri: str
    interval_seconds: float | None = None
    expires_in_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class OAuthPrompt:


    message: str
    placeholder: str | None = None
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class OAuthSelectOption:


    id: str
    label: str


@dataclass(frozen=True, slots=True)
class OAuthSelectPrompt:


    message: str
    options: tuple[OAuthSelectOption, ...]


@dataclass(frozen=True, slots=True)
class OAuthRuntimeAuth:


    api_key: str
    base_url: str | None = None
    headers: Mapping[str, str] | None = None


AuthCallback = Callable[[OAuthAuthInfo], None]
DeviceCodeCallback = Callable[[OAuthDeviceCodeInfo], None]
PromptCallback = Callable[[OAuthPrompt], Awaitable[str]]
SelectCallback = Callable[[OAuthSelectPrompt], Awaitable[str | None]]
ManualCodeCallback = Callable[[], Awaitable[str]]
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class OAuthLoginCallbacks:


    on_auth: AuthCallback
    on_device_code: DeviceCodeCallback
    on_prompt: PromptCallback
    on_select: SelectCallback
    on_progress: ProgressCallback | None = None
    on_manual_code_input: ManualCodeCallback | None = None
    method: OAuthFlowKind | None = None


class OAuthProvider(Protocol):


    @property
    def id(self) -> str:

        ...

    @property
    def name(self) -> str:

        ...

    @property
    def flow_kinds(self) -> Sequence[OAuthFlowKind]:

        ...

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredential:

        ...

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:

        ...

    def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:

        ...


def oauth_metadata_string(
    metadata: Mapping[str, JSONValue],
    name: str,
) -> str | None:

    value = metadata.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
