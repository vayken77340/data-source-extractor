"""Layer 3b: auth. Importing this registers every shipped strategy."""

from __future__ import annotations

from api_extractor.auth import strategies  # noqa: F401  registers by being imported
from api_extractor.auth.registry import Authenticator, acquire, registered

__all__ = ["Authenticator", "acquire", "registered"]
