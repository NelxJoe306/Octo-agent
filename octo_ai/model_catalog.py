from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from octo_ai.model_limits import RuntimeModelLimits

RuntimeInputModality = Literal["text", "image"]
RuntimeThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True, slots=True)
class RuntimeModel:


    id: str
    name: str | None = None
    limits: RuntimeModelLimits | None = None
    input_modalities: tuple[RuntimeInputModality, ...] = ("text",)
    thinking_levels: tuple[RuntimeThinkingLevel, ...] = ()
    default_thinking_level: RuntimeThinkingLevel | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("model id must be non-empty")
        if not self.input_modalities:
            raise ValueError("input_modalities must be non-empty")
        if self.default_thinking_level is not None and (
            self.default_thinking_level not in self.thinking_levels
        ):
            raise ValueError("default_thinking_level must be advertised in thinking_levels")


@dataclass(frozen=True, slots=True)
class RuntimeModelCatalog:


    models: tuple[RuntimeModel, ...]

    def model(self, model_id: str) -> RuntimeModel | None:

        return next((model for model in self.models if model.id == model_id), None)


@runtime_checkable
class ModelCatalogProvider(Protocol):


    async def discover_models(self) -> RuntimeModelCatalog:

        ...
