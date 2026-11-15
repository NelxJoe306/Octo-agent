from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from agent_core.provider import ModelProvider
from terminal_app.session import CodingSession, CodingSessionConfig


@dataclass(frozen=True, slots=True)
class SessionPreparationRequest:


    storage: object
    destination_cwd: Path
    model: str
    provider: str | None = None
    session_provider: str | None = None
    session_id: str | None = None


@dataclass(slots=True)
class PreparedCodingSession:


    session: CodingSession
    _state: str = "prepared"

    @property
    def provider(self) -> ModelProvider:
        return self.session.provider

    async def adopt(self) -> CodingSession:

        if self._state != "prepared":
            raise RuntimeError(f"Prepared session is already {self._state}")
        trust_resolution = getattr(self.session, "project_trust_resolution", None)
        if trust_resolution is not None and trust_resolution.cancelled:
            await self.abort()
            raise ValueError("Project trust decision cancelled; session was not adopted")
        try:
            commit = getattr(self.session, "_commit_prepared_entries", None)
            if commit is not None:
                await commit()
        except BaseException:
            await self.abort()
            raise
        self._state = "adopted"
        activate = getattr(self.session, "activate_memory", None)
        if activate is not None:
            activate()
        return self.session

    async def abort(self) -> None:

        if self._state != "prepared":
            return
        self._state = "aborted"
        close = getattr(self.session, "aclose", None)
        if close is not None:
            await close()


async def prepare_coding_session(
    config: CodingSessionConfig,
    *,
    session_loader: type[CodingSession] | None = None,
) -> PreparedCodingSession:


    staged_config = replace(config, defer_authoritative_writes=True)
    loader = session_loader or CodingSession
    session = await loader.load(staged_config)
    return PreparedCodingSession(session)


__all__ = [
    "PreparedCodingSession",
    "SessionPreparationRequest",
    "prepare_coding_session",
]
