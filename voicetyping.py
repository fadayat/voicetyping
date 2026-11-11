#!/usr/bin/env python3
"""Developer launcher for the VoiceTyping source package."""

from pathlib import Path
import importlib.util
import sys


SOURCE_ROOT = Path(__file__).resolve().parent / "src"
PACKAGE_ROOT = SOURCE_ROOT / "voicetyping"
sys.path.insert(0, str(SOURCE_ROOT))
__path__ = [str(PACKAGE_ROOT)]

# This root file shadows the src package when imported from the checkout.
# Load the package's version declaration so both direct imports and the app
# module see the same canonical value without copying it here.
_spec = importlib.util.spec_from_file_location(
    "_voicetyping_package_metadata", PACKAGE_ROOT / "__init__.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load VoiceTyping package metadata")
_metadata = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_metadata)
__version__ = _metadata.__version__

if __name__ == "__main__":
    from voicetyping.app import main

    raise SystemExit(main())
