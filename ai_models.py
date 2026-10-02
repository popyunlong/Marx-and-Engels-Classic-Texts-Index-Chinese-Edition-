"""Stable application identities and current provider API names.

Keep the legacy Flash identity in browser/entitlement/billing contracts so an
open page also works across a release rollback. Only the provider wire name
changes; persisted usage rows are never renamed.
"""

FLASH_MODEL = "deepseek-v4-flash"
FLASH_API_MODEL = "deepseek-flash"
FLASH_ALIASES = frozenset({FLASH_MODEL, FLASH_API_MODEL, "deepseek-v4-flash-vision-exp"})


def application_model(value: object) -> str:
    model = str(value or "").strip().lower()
    return FLASH_MODEL if model in FLASH_ALIASES else model


def provider_model(value: object) -> str:
    model = str(value or "").strip()
    return FLASH_API_MODEL if model.lower() in FLASH_ALIASES else model
