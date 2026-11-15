from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_core.types import JSONValue
from terminal_app.paths import OctoPaths


class ResourceError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SystemPromptResources:


    custom_prompt: str | None = None
    custom_prompt_path: Path | None = None
    append_prompt: str | None = None
    append_prompts: tuple[str, ...] = ()
    append_prompt_paths: tuple[Path, ...] = ()
    diagnostics: tuple[ResourceDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceDiagnostic:


    kind: str
    message: str
    path: Path | None = None
    name: str | None = None
    severity: str = "warning"

    def format(self) -> str:

        parts = [self.severity, self.kind]
        if self.name is not None:
            parts.append(self.name)
        label = " ".join(parts)
        if self.path is None:
            return f"{label}: {self.message}"
        return f"{label}: {self.message} ({self.path})"


@dataclass(frozen=True, slots=True)
class OctoResourcePaths:


    root: Path = field(default_factory=lambda: OctoPaths().home)
    cwd: Path | None = None
    agents_root: Path | None = field(default_factory=lambda: Path.home() / ".agents")
    paths: OctoPaths | None = None
    project_resources_enabled: bool = True

    @property
    def skills_dir(self) -> Path:

        return self.root / "skills"

    @property
    def prompts_dir(self) -> Path:

        return self.root / "prompts"

    @property
    def system_prompt_path(self) -> Path:

        return self.root / "SYSTEM.md"

    @property
    def append_system_prompt_path(self) -> Path:

        return self.root / "APPEND_SYSTEM.md"

    @property
    def skills_dirs(self) -> tuple[Path, ...]:

        paths = self._paths()
        dirs = [self.skills_dir]
        if self.agents_root is not None:
            dirs.append(self.agents_root / "skills")
        if self.cwd is not None and self.project_resources_enabled:
            dirs.extend(
                [
                    paths.project_skills_dir(self.cwd),
                    paths.project_agents_skills_dir(self.cwd),
                ]
            )
        return tuple(_dedupe_paths(dirs))

    @property
    def themes_dirs(self) -> tuple[Path, ...]:

        paths = self._paths()
        dirs = [self.root / "themes"]
        if self.cwd is not None and self.project_resources_enabled:
            dirs.append(paths.project_themes_dir(self.cwd))
        return tuple(_dedupe_paths(dirs))

    @property
    def prompts_dirs(self) -> tuple[Path, ...]:

        paths = self._paths()
        dirs = [self.prompts_dir]
        if self.agents_root is not None:
            dirs.append(self.agents_root / "prompts")
        if self.cwd is not None and self.project_resources_enabled:
            dirs.extend(
                [
                    paths.project_prompts_dir(self.cwd),
                    paths.project_agents_prompts_dir(self.cwd),
                ]
            )
        return tuple(_dedupe_paths(dirs))

    def _paths(self) -> OctoPaths:
        agents_home = self.agents_root or Path.home() / ".agents"
        return self.paths or OctoPaths(home=self.root, agents_home=agents_home)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def discover_system_prompt_resources(
    paths: OctoResourcePaths,
    *,
    custom_prompt_explicit: bool = False,
    enabled: bool = True,
) -> SystemPromptResources:

    if not enabled:
        return SystemPromptResources()

    diagnostics: list[ResourceDiagnostic] = []
    custom_prompt, custom_path = _discover_system_prompt_file(
        paths,
        filename="SYSTEM.md",
        label="replacement",
        explicit=custom_prompt_explicit,
        diagnostics=diagnostics,
    )
    append_prompts, append_paths = _discover_append_system_prompt_files(
        paths,
        diagnostics=diagnostics,
    )
    return SystemPromptResources(
        custom_prompt=custom_prompt,
        custom_prompt_path=custom_path,
        append_prompt="\n\n".join(append_prompts) if append_prompts else None,
        append_prompts=append_prompts,
        append_prompt_paths=append_paths,
        diagnostics=tuple(diagnostics),
    )


def _discover_system_prompt_file(
    paths: OctoResourcePaths,
    *,
    filename: str,
    label: str,
    explicit: bool,
    diagnostics: list[ResourceDiagnostic],
) -> tuple[str | None, Path | None]:
    candidates: list[tuple[str, Path]] = []
    if paths.cwd is not None and paths.project_resources_enabled:
        candidates.append(("project", paths.cwd / ".octo" / filename))
    candidates.append(("user", paths.root / filename))

    existing: list[tuple[str, Path]] = []
    for scope, path in candidates:
        try:
            if path.exists():
                existing.append((scope, path))
        except OSError as exc:
            raise ResourceError(
                f"Could not inspect {label} system prompt file {path}: {exc}"
            ) from exc

    if explicit:
        for _scope, path in existing:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="system-prompt",
                    name=label,
                    path=path,
                    severity="info",
                    message="ignored because an explicit startup value takes precedence",
                )
            )
        return None, None
    if not existing:
        return None, None

    selected_scope, selected_path = existing[0]
    try:
        content = selected_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ResourceError(
            f"Could not read {label} system prompt file {selected_path} as UTF-8: {exc}"
        ) from exc

    diagnostics.append(
        ResourceDiagnostic(
            kind="system-prompt",
            name=label,
            path=selected_path,
            severity="info",
            message=f"selected {selected_scope} system prompt file",
        )
    )
    for _scope, path in existing[1:]:
        diagnostics.append(
            ResourceDiagnostic(
                kind="system-prompt",
                name=label,
                path=path,
                message=f"shadowed by higher-precedence file {selected_path}",
            )
        )
    return content, selected_path


def _discover_append_system_prompt_files(
    paths: OctoResourcePaths,
    *,
    diagnostics: list[ResourceDiagnostic],
) -> tuple[tuple[str, ...], tuple[Path, ...]]:

    candidates: list[tuple[str, Path]] = [("user", paths.root / "APPEND_SYSTEM.md")]
    if paths.cwd is not None and paths.project_resources_enabled:
        candidates.append(("project", paths.cwd / ".octo" / "APPEND_SYSTEM.md"))

    contents: list[str] = []
    selected_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for scope, path in candidates:
        try:
            resolved_path = path.expanduser().resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            exists = path.exists()
        except OSError as exc:
            raise ResourceError(
                f"Could not inspect append system prompt file {path}: {exc}"
            ) from exc
        if not exists:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ResourceError(
                f"Could not read append system prompt file {path} as UTF-8: {exc}"
            ) from exc
        contents.append(content)
        selected_paths.append(path)
        diagnostics.append(
            ResourceDiagnostic(
                kind="system-prompt",
                name="append",
                path=path,
                severity="info",
                message=f"selected {scope} system prompt file",
            )
        )

    return tuple(contents), tuple(selected_paths)


def resource_paths_with_cwd(
    paths: OctoResourcePaths | None,
    cwd: Path,
) -> OctoResourcePaths:

    if paths is None:
        return OctoResourcePaths(cwd=cwd)


    return OctoResourcePaths(
        root=paths.root,
        cwd=cwd,
        agents_root=paths.agents_root,
        paths=paths.paths,
        project_resources_enabled=paths.project_resources_enabled,
    )


def resource_paths_with_project_trust(
    paths: OctoResourcePaths,
    *,
    trusted: bool,
) -> OctoResourcePaths:

    return OctoResourcePaths(
        root=paths.root,
        cwd=paths.cwd,
        agents_root=paths.agents_root,
        paths=paths.paths,
        project_resources_enabled=trusted,
    )


def parse_markdown_resource(text: str) -> tuple[dict[str, str], str]:

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized

    end = normalized.find("\n---", 4)
    if end == -1:
        return {}, normalized

    raw_frontmatter = normalized[4:end]
    body = normalized[end + len("\n---") :]
    if body.startswith("\n"):
        body = body[1:]

    metadata: dict[str, str] = {}
    for line in raw_frontmatter.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, body


def derive_description(content: str) -> str | None:

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
        return stripped
    return None


def metadata_to_json(metadata: dict[str, str]) -> dict[str, JSONValue]:

    return dict(metadata)
