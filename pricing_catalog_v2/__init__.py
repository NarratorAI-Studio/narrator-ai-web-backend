"""Hard-price v2 catalog persistence (regression coverage / narrator-ai-Web API contract).

Implements the schema and read surface for the tier list defined in
narrator-ai-web's `docs/pricing/v2/catalog-tier-contract.md` (frozen
2026-05-29 in web review). The write surface and operator UX live
behind Web API contract's subsequent update backend PR.

v1's fa_template_price stays in place for HP-XX quote/pin compatibility.
v2 lives in three new tables and is additive.
"""

from __future__ import annotations

from .errors import (
    CatalogFlashProAxisViolation,
    CatalogInheritedInvariantViolation,
    CatalogPersistenceError,
    CatalogProBelowFlash,
    CatalogProSurchargeMismatch,
    CatalogRoundingVersionUnknown,
    CatalogTemplateNotFound,
    CatalogTierMissing,
    CatalogValidationError,
)
from .store import (
    EffectiveTier,
    TemplateMetadata,
    get_family_tiers,
    get_template_metadata,
    get_template_tiers,
    list_all_versions,
    resolve_effective_tiers,
    upsert_tiers,
)


__all__ = [
    "CatalogFlashProAxisViolation",
    "CatalogInheritedInvariantViolation",
    "CatalogPersistenceError",
    "CatalogProBelowFlash",
    "CatalogProSurchargeMismatch",
    "CatalogRoundingVersionUnknown",
    "CatalogTemplateNotFound",
    "CatalogTierMissing",
    "CatalogValidationError",
    "EffectiveTier",
    "TemplateMetadata",
    "get_family_tiers",
    "get_template_metadata",
    "get_template_tiers",
    "list_all_versions",
    "resolve_effective_tiers",
    "upsert_tiers",
]
