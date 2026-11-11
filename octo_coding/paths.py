from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256
from os import environ
from pathlib import Path


# 统一从 OCTO_HOME 解析应用状态目录，默认使用独立的 .octo。
def _default_octo_home() -> Path:

    value = environ.get("OCTO_HOME")
    if value is None or value == "":
        return Path.home() / ".octo"

    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise ValueError(
            "OCTO_HOME could not expand '~'; use an absolute path or an existing user home"
        ) from exc
    if not path.is_absolute():
        raise ValueError("OCTO_HOME must be an absolute path after '~' expansion")
    return path


@dataclass(frozen=True, slots=True)
class OctoPaths:


    home: Path = field(default_factory=_default_octo_home)
    agents_home: Path = field(default_factory=lambda: Path.home() / ".agents")

    @property
    def sessions_dir(self) -> Path:

        return self.home / "sessions"

    @property
    def logs_dir(self) -> Path:

        return self.home / "logs"

    @property
    def agent_calls_log_path(self) -> Path:

        return self.logs_dir / "agent-calls.jsonl"

    @property
    def models_store_path(self) -> Path:

        return self.home / "models-store.json"

    @property
    def codex_version_store_path(self) -> Path:

        return self.home / "codex-version-store.json"

    @property
    def codex_models_store_path(self) -> Path:

        return self.home / "codex-models-store.json"

    @property
    def extension_state_dir(self) -> Path:

        return self.home / "state" / "extensions"

    @property
    def llama_cpp_state_path(self) -> Path:

        return self.extension_state_dir / "llama.cpp.json"

    @property
    def user_skills_dir(self) -> Path:

        return self.home / "skills"

    @property
    def user_prompts_dir(self) -> Path:

        return self.home / "prompts"

    @property
    def user_themes_dir(self) -> Path:

        return self.home / "themes"

    @property
    def user_extensions_dir(self) -> Path:

        return self.home / "extensions"

    @property
    def user_agents_skills_dir(self) -> Path:

        return self.agents_home / "skills"

    @property
    def user_agents_prompts_dir(self) -> Path:

        return self.agents_home / "prompts"

    def project_octo_dir(self, cwd: Path) -> Path:

        return cwd / ".octo"

    def project_agents_dir(self, cwd: Path) -> Path:

        return cwd / ".agents"

    def project_skills_dir(self, cwd: Path) -> Path:

        return self.project_octo_dir(cwd) / "skills"

    def project_prompts_dir(self, cwd: Path) -> Path:

        return self.project_octo_dir(cwd) / "prompts"

    def project_themes_dir(self, cwd: Path) -> Path:

        return self.project_octo_dir(cwd) / "themes"

    def project_agents_skills_dir(self, cwd: Path) -> Path:

        return self.project_agents_dir(cwd) / "skills"

    def project_agents_prompts_dir(self, cwd: Path) -> Path:

        return self.project_agents_dir(cwd) / "prompts"

    def project_session_dir(self, cwd: Path) -> Path:

        resolved = cwd.resolve()
        digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
        slug = _slugify_path(resolved)
        return self.sessions_dir / f"{slug or 'project'}-{digest}"

    def default_session_path(self, cwd: Path) -> Path:

        path = self.project_session_dir(cwd) / "default.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def _slugify_path(path: Path, *, max_length: int = 72) -> str:
    parts = [part for part in path.parts if part not in (path.anchor, "")]
    try:
        relative_to_home = path.relative_to(Path.home())
    except ValueError:
        pass
    else:
        parts = ["home", *relative_to_home.parts]

    slug_parts = [
        normalized
        for part in parts
        if (normalized := re.sub(r"[^a-zA-Z0-9._-]+", "-", part).strip(".-_").lower())
    ]
    slug = "-".join(slug_parts)
    if len(slug) <= max_length:
        return slug

    suffix_parts: list[str] = []
    suffix_length = 0
    for part in reversed(slug_parts):
        next_length = suffix_length + len(part) + (1 if suffix_parts else 0)
        if next_length > max_length:
            break
        suffix_parts.append(part)
        suffix_length = next_length
    return "-".join(reversed(suffix_parts)) or slug[-max_length:].strip("-")
