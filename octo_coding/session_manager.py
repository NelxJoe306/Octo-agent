from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from octo_coding.paths import OctoPaths

_MAX_SESSION_ID_BYTES = 128
_RESERVED_SESSION_IDS = frozenset({"default", "index"})
_WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

InferenceProviderMode = Literal["automatic", "fixed"]


def normalize_session_name(value: str) -> str:

    name = value.strip()
    if not name:
        raise ValueError("Session name cannot be empty")
    if any(char in name for char in "\r\n\t"):
        raise ValueError("Session name must be a single line.")
    return name


def validate_session_id(session_id: str) -> None:

    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "Session id must be non-empty, contain only alphanumeric characters, '-', '_', "
            "and '.', and start and end with an alphanumeric character"
        )
    if len(session_id.encode("utf-8")) > _MAX_SESSION_ID_BYTES:
        raise ValueError(f"Session id must be at most {_MAX_SESSION_ID_BYTES} bytes")
    normalized_id = session_id.casefold()
    if normalized_id in _RESERVED_SESSION_IDS:
        raise ValueError(f"Session id is reserved: {session_id}")
    if normalized_id.partition(".")[0] in _WINDOWS_RESERVED_FILE_STEMS:
        raise ValueError(f"Session id is not a portable file name: {session_id}")


class SessionRecordModel(BaseModel):


    model_config = ConfigDict(extra="ignore")

    id: str
    path: str
    cwd: str
    model: str
    provider_name: str | None = None
    inference_provider: str | None = None
    inference_provider_mode: InferenceProviderMode | None = None
    title: str | None = None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class CodingSessionRecord:


    id: str
    path: Path
    cwd: Path
    model: str
    title: str | None
    created_at: float
    updated_at: float
    provider_name: str | None = None
    inference_provider: str | None = None
    inference_provider_mode: InferenceProviderMode = "automatic"

    @classmethod
    def from_model(cls, model: SessionRecordModel) -> CodingSessionRecord:

        return cls(
            id=model.id,
            path=Path(model.path),
            cwd=Path(model.cwd),
            model=model.model,
            title=model.title,
            created_at=model.created_at,
            updated_at=model.updated_at,
            provider_name=model.provider_name,
            inference_provider=model.inference_provider,
            inference_provider_mode=(
                model.inference_provider_mode
                or ("fixed" if model.inference_provider is not None else "automatic")
            ),
        )

    def to_model(self) -> SessionRecordModel:

        return SessionRecordModel(
            id=self.id,
            path=str(self.path),
            cwd=str(self.cwd),
            model=self.model,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            provider_name=self.provider_name,
            inference_provider=self.inference_provider,
            inference_provider_mode=self.inference_provider_mode,
        )


class SessionManager:


    def __init__(self, paths: OctoPaths | None = None) -> None:
        self.paths = paths or OctoPaths()

    @property
    def index_path(self) -> Path:

        return self.paths.sessions_dir / "index.jsonl"

    def project_index_path(self, cwd: Path) -> Path:

        return self.paths.project_session_dir(cwd) / "index.jsonl"

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:

        records = self._read_project_records(cwd) if cwd is not None else self._read_all_records()
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def get_session(self, session_id: str) -> CodingSessionRecord | None:

        for record in self._read_all_records():
            if record.id == session_id:
                return record
        return None

    def latest_session_for_cwd(self, cwd: Path) -> CodingSessionRecord | None:

        records = self.list_sessions(cwd)
        return records[0] if records else None

    def create_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:

        record = self.prepare_session(
            cwd=cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=inference_provider_mode,
            title=title,
            session_id=session_id,
        )
        self.index_session(record)
        return record

    def create_session_exclusive(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:

        record = self.prepare_session(
            cwd=cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=inference_provider_mode,
            title=title,
            session_id=session_id,
        )
        if self.get_session(record.id) is not None:
            raise RuntimeError(f"Session already exists with id '{record.id}'")
        try:
            with record.path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as exc:
            raise RuntimeError(f"Session already exists with id '{record.id}'") from exc
        except Exception:
            with suppress(OSError):
                record.path.unlink(missing_ok=True)
            raise
        try:
            return self.index_session(record)
        except Exception:
            with suppress(Exception):
                self._remove(record)
            with suppress(OSError):
                record.path.unlink(missing_ok=True)
            raise

    def prepare_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:

        now = time()
        resolved_cwd = cwd.resolve()
        record_id = uuid4().hex if session_id is None else session_id
        validate_session_id(record_id)
        project_session_dir = self.paths.project_session_dir(resolved_cwd)
        default_session_id = f"default-{project_session_dir.name}"
        if record_id.casefold() == default_session_id.casefold():
            raise ValueError(f"Session id is reserved: {record_id}")
        path = project_session_dir / f"{record_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return CodingSessionRecord(
            id=record_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=(
                inference_provider_mode
                or ("fixed" if inference_provider is not None else "automatic")
            ),
            title=title,
            created_at=now,
            updated_at=now,
        )

    def index_session(self, record: CodingSessionRecord) -> CodingSessionRecord:

        self._upsert(record)
        return record

    def get_or_create_default_session(
        self, *, cwd: Path, model: str, provider_name: str | None = None
    ) -> CodingSessionRecord:

        resolved_cwd = cwd.resolve()
        project_hash = self.paths.project_session_dir(resolved_cwd).name
        session_id = f"default-{project_hash}"
        existing = self.get_session(session_id)
        if existing is not None:
            return existing

        now = time()
        path = self.paths.default_session_path(resolved_cwd)
        record = CodingSessionRecord(
            id=session_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            title="Default session",
            created_at=now,
            updated_at=now,
        )
        self._upsert(record)
        return record

    def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        preserve_inference_provider: bool = True,
        title: str | None = None,
    ) -> CodingSessionRecord | None:

        existing = self.get_session(session_id)
        if existing is None:
            return None
        updated = CodingSessionRecord(
            id=existing.id,
            path=existing.path,
            cwd=existing.cwd,
            model=model or existing.model,
            provider_name=provider_name if provider_name is not None else existing.provider_name,
            inference_provider=(
                existing.inference_provider if preserve_inference_provider else inference_provider
            ),
            inference_provider_mode=(
                existing.inference_provider_mode
                if preserve_inference_provider or inference_provider_mode is None
                else inference_provider_mode
            ),
            title=title if title is not None else existing.title,
            created_at=existing.created_at,
            updated_at=time(),
        )
        self._upsert(updated)
        return updated

    def _read_index(self, path: Path) -> list[CodingSessionRecord]:
        if not path.exists():
            return []

        records: list[CodingSessionRecord] = []


        for line in path.read_text(encoding="utf-8").split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            model = SessionRecordModel.model_validate_json(stripped)
            records.append(CodingSessionRecord.from_model(model))
        return records

    def _read_project_records(self, cwd: Path) -> list[CodingSessionRecord]:
        resolved_cwd = cwd.resolve()
        records = self._read_index(self.project_index_path(resolved_cwd))
        records.extend(
            record for record in self._read_index(self.index_path) if record.cwd == resolved_cwd
        )
        return _deduplicate_records(records)

    def _read_all_records(self) -> list[CodingSessionRecord]:
        records = self._read_index(self.index_path)
        for index_path in self.paths.sessions_dir.glob("*/index.jsonl"):
            records.extend(self._read_index(index_path))
        return _deduplicate_records(records)

    def _write_index(self, path: Path, records: list[CodingSessionRecord]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(record.to_model().model_dump_json() for record in records)
        if content:
            content += "\n"
        path.write_text(content, encoding="utf-8")

    def _upsert(self, record: CodingSessionRecord) -> None:
        path = self.project_index_path(record.cwd)
        records = [item for item in self._read_index(path) if item.id != record.id]
        records.append(record)
        self._write_index(path, records)

    def _remove(self, record: CodingSessionRecord) -> None:
        path = self.project_index_path(record.cwd)
        records = [item for item in self._read_index(path) if item.id != record.id]
        self._write_index(path, records)


def _deduplicate_records(records: list[CodingSessionRecord]) -> list[CodingSessionRecord]:
    by_id: dict[str, CodingSessionRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None or record.updated_at >= existing.updated_at:
            by_id[record.id] = record
    return list(by_id.values())
