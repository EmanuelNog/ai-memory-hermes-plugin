from __future__ import annotations

import os
import sys
from pathlib import Path

_init_dir = str(Path(__file__).resolve().parent)
if _init_dir not in sys.path:
    sys.path.insert(0, _init_dir)

# Hermes loads __init__.py as a standalone module (not a package), so
# relative imports (from .foo import Bar) fail. Bare imports + sys.path
# ensure sibling modules are found regardless of how the plugin is loaded.
from config import load_config  # noqa: E402
from provider import AiMemoryProvider  # noqa: E402

__all__ = ["register"]


def register(ctx: object) -> None:
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    # Share config validation/env precedence with initialize() and the CLI.
    config = load_config(hermes_home)

    provider = AiMemoryProvider(config=config)
    ctx.register_memory_provider(provider)  # type: ignore[union-attr]
