from __future__ import annotations

from dataclasses import dataclass, field
from json import dumps, loads
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol

from octo_agent.types import JSONValue
from octo_coding.paths import OctoPaths


class CredentialStoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthCredential:


    access: str
    refresh: str
    expires: int
    account_id: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def to_json(self) -> dict[str, JSONValue]:

        result: dict[str, JSONValue] = {
            "type": "oauth",
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
        }
        if self.account_id is not None:
            result["account_id"] = self.account_id
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass(frozen=True, slots=True)
class ApiKeyCredential:


    key: str

    def to_json(self) -> dict[str, JSONValue]:

        return {"type": "api_key", "key": self.key}


type StoredCredential = str | ApiKeyCredential | OAuthCredential
type StoredCredentialKind = Literal["api_key", "oauth"]


class CredentialStore(Protocol):


    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def names(self, *, prefix: str | None = None) -> tuple[str, ...]: ...


class FileCredentialStore:


    def __init__(self, path: Path | None = None) -> None:
        self.path = path or credentials_path()

    def get(self, name: str) -> str | None:

        credential = self._load().get(name)
        if isinstance(credential, str):
            return credential
        if isinstance(credential, ApiKeyCredential):
            return credential.key
        return None

    def set(self, name: str, value: str) -> None:

        name = _validate_credential_name(name)
        value = value.strip()
        if not value:
            raise CredentialStoreError("Credential value must not be empty")
        data = self._load()
        data[name] = value
        self._save(data)

    def set_api_key(self, name: str, value: str) -> None:

        self.set(name, value)

    def get_oauth(self, name: str) -> OAuthCredential | None:

        credential = self._load().get(name)
        if isinstance(credential, OAuthCredential):
            return credential
        return None

    def set_oauth(self, name: str, credential: OAuthCredential) -> None:

        name = _validate_credential_name(name)
        _validate_oauth_credential(credential)
        data = self._load()
        data[name] = credential
        self._save(data)

    def delete(self, name: str) -> None:

        data = self._load()
        data.pop(name, None)
        self._save(data)

    def names(self, *, prefix: str | None = None) -> tuple[str, ...]:

        names = tuple(sorted(self._load()))
        if prefix is None:
            return names
        return tuple(name for name in names if name.startswith(prefix))

    def _load(self) -> dict[str, StoredCredential]:
        if not self.path.exists():
            return {}
        raw = loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CredentialStoreError("Octo credentials must be a JSON object")
        credentials: dict[str, StoredCredential] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise CredentialStoreError("Octo credential names must be strings")
            credentials[key] = _credential_from_json(value)
        return credentials

    def _save(self, data: dict[str, StoredCredential]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {key: _credential_to_json(value) for key, value in data.items()}
        content = dumps(raw, indent=2, sort_keys=True) + "\n"
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                temporary_path.chmod(0o600)
                handle.write(content)
                handle.flush()
            temporary_path.replace(self.path)
            self.path.chmod(0o600)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def credentials_path(paths: OctoPaths | None = None) -> Path:

    return (paths or OctoPaths()).home / "credentials.json"


def _validate_credential_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise CredentialStoreError("Credential name must not be empty")
    return normalized


def _validate_oauth_credential(credential: OAuthCredential) -> None:
    if not credential.access.strip():
        raise CredentialStoreError("OAuth access token must not be empty")
    if not credential.refresh.strip():
        raise CredentialStoreError("OAuth refresh token must not be empty")
    if credential.account_id is not None and not credential.account_id.strip():
        raise CredentialStoreError("OAuth account id must not be empty")
    if credential.expires <= 0:
        raise CredentialStoreError("OAuth expiry must be greater than 0")
    _validate_oauth_metadata(credential.metadata)


def _credential_from_json(value: object) -> StoredCredential:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise CredentialStoreError("Octo credential values must be strings or objects")

    credential_type = value.get("type")
    if credential_type not in {"api_key", "oauth"}:
        raise CredentialStoreError("Octo credential object type must be api_key or oauth")
    if credential_type == "api_key":
        key = _string_field(value, "key", credential_type)
        return ApiKeyCredential(key=key)

    expires = value.get("expires")
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        raise CredentialStoreError("Octo oauth credential expires must be a positive integer")
    account_id = value.get("account_id")
    if account_id is not None and (not isinstance(account_id, str) or not account_id.strip()):
        raise CredentialStoreError("Octo oauth credential account_id must be a non-empty string")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise CredentialStoreError("Octo oauth credential metadata must be an object")
    _validate_oauth_metadata(metadata)
    return OAuthCredential(
        access=_string_field(value, "access", credential_type),
        refresh=_string_field(value, "refresh", credential_type),
        expires=expires,
        account_id=account_id,
        metadata=dict(metadata),
    )


def _credential_to_json(value: StoredCredential) -> str | dict[str, JSONValue]:
    if isinstance(value, str):
        return value
    return value.to_json()


def _validate_oauth_metadata(metadata: dict[Any, Any]) -> None:
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise CredentialStoreError("Octo oauth credential metadata keys must be strings")
        if not _is_json_value(value):
            raise CredentialStoreError("Octo oauth credential metadata values must be JSON values")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int | float):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _string_field(
    value: dict[Any, Any],
    field_name: str,
    credential_type: StoredCredentialKind,
) -> str:
    field = value.get(field_name)
    if not isinstance(field, str) or not field.strip():
        raise CredentialStoreError(
            f"Octo {credential_type} credential field must be a non-empty string: {field_name}"
        )
    return field.strip()
