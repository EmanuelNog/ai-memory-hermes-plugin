"""Regression: a foreign top-level ``config`` module must not break the plugin.

chk-j3si.154 — the plugin used to import its config as the bare module name
``config``.  Any plugin that had already published ``sys.modules['config']``
(e.g. hermes-jev) made ``from config import load_config`` fail, so ``register()``
never reached ``ctx.register_memory_provider()`` and Hermes reported
"loaded but no provider instance found".

The plugin now imports ``aimemory_config``.  This test loads it in a *fresh*
interpreter with a hostile ``config`` module installed and asserts that
registration still succeeds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent / "plugins/memory/ai-memory"


def test_plugin_loads_with_foreign_config_module(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import sys, types
        fake = types.ModuleType("config")
        fake.__file__ = "<foreign-config.py>"
        # Deliberately does NOT expose load_config / AiMemoryConfig.
        sys.modules["config"] = fake
        sys.path.insert(0, {str(PLUGIN_DIR)!r})
        import __init__
        class Ctx:
            def __init__(self):
                self.provider = None
            def register_memory_provider(self, provider):
                self.provider = provider
        ctx = Ctx()
        __init__.register(ctx)
        assert ctx.provider is not None, "register() did not register a provider"
        print("SHADOW-TEST-OK")
        """
    )
    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "SHADOW-TEST-OK" in result.stdout
