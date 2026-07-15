"""Public import surface for the offline DreamDEX SIWE authentication model.

This module intentionally re-exports only the in-memory models, fixture
transport/signer, and fail-closed state machine from ``dreamdex_auth_models``.
It contains no production key loader and performs no network I/O by itself.
"""
from bot.integrations.dreamdex_auth_models import *  # noqa: F401,F403
