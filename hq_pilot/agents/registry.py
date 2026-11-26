"""Registry for pluggable CodingAgentAdapter implementations."""

from __future__ import annotations

import importlib
from typing import Any, Callable, Dict, List, Optional, Type

from hq_pilot.agents.base import CodingAgentAdapter

_ADAPTER_REGISTRY: Dict[str, Type[CodingAgentAdapter]] = {}
_BUILTINS_LOADED = False


def register_agent_adapter(
    name: str,
) -> Callable[[Type[CodingAgentAdapter]], Type[CodingAgentAdapter]]:
    """Decorator registering a CodingAgentAdapter subclass under `name`."""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("Agent adapter registry name must not be empty")

    def _decorator(cls: Type[CodingAgentAdapter]) -> Type[CodingAgentAdapter]:
        if not (isinstance(cls, type) and issubclass(cls, CodingAgentAdapter)):
            raise TypeError(
                f"Registered adapter {cls!r} must be a subclass of CodingAgentAdapter"
            )
        _ADAPTER_REGISTRY[key] = cls
        return cls

    return _decorator


def _ensure_builtins_registered() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from hq_pilot.agents.agy import AgyAdapter
    from hq_pilot.agents.codex import CodexAdapter

    _ADAPTER_REGISTRY.setdefault("agy", AgyAdapter)
    _ADAPTER_REGISTRY.setdefault("codex", CodexAdapter)


def list_agent_backends() -> List[str]:
    """Return sorted names of all registered coding agent backends."""
    _ensure_builtins_registered()
    return sorted(_ADAPTER_REGISTRY.keys())


def _load_dynamic_adapter_class(spec: str) -> Type[CodingAgentAdapter]:
    module_name, class_name = spec.split(":", 1)
    module_name = module_name.strip()
    class_name = class_name.strip()
    if not module_name or not class_name:
        raise ValueError(
            f"Invalid coding agent backend spec {spec!r}; expected 'module.path:ClassName'"
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"Failed to import coding agent adapter module {module_name!r} from {spec!r}: {exc}"
        ) from exc

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(
            f"Coding agent adapter class {class_name!r} not found in module {module_name!r}"
        )
    if not (isinstance(cls, type) and issubclass(cls, CodingAgentAdapter)):
        raise TypeError(
            f"Coding agent adapter {spec!r} must inherit from CodingAgentAdapter"
        )
    return cls


def get_agent_adapter(
    backend: str,
    bin_path: Optional[str] = None,
    **kwargs: Any,
) -> CodingAgentAdapter:
    """Resolve and instantiate a CodingAgentAdapter by registry name or `module:Class`."""
    raw = (backend or "agy").strip()
    if not raw:
        raw = "agy"

    if ":" in raw:
        cls = _load_dynamic_adapter_class(raw)
    else:
        _ensure_builtins_registered()
        key = raw.lower()
        cls = _ADAPTER_REGISTRY.get(key)
        if cls is None:
            available = ", ".join(list_agent_backends())
            raise ValueError(
                f"Unsupported coding agent backend: {backend!r}. "
                f"Available backends: {available} (or pass 'module.path:ClassName')"
            )

    init_kwargs: Dict[str, Any] = dict(kwargs)
    if bin_path is not None:
        init_kwargs["bin_path"] = bin_path
    return cls(**init_kwargs)
