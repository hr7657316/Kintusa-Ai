"""Kintsua: self-maintaining integrations for AI agents.

``import kintsua`` gives you the runtime SDK (``wrap_mcp``, ``wrap_http``, ``hook_*``);
``kintsua watch`` is the open-source CLI. The hosted service lives in the other
subpackages and needs ``pip install "kintsua[server]"``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "wrap_mcp",
    "wrap_http",
    "hook_langchain",
    "hook_openai_agents",
    "hook_google_adk",
    "configure",
    "flush",
]


def __getattr__(name: str):
    # Lazy so importing the package never pulls the SDK's reporter thread unless used.
    if name in __all__ and name != "__version__":
        from kintsua import sdk

        return getattr(sdk, name)
    raise AttributeError(name)
