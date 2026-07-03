"""Exceptions raised by the v2 catalog persistence layer."""

from __future__ import annotations


class CatalogPersistenceError(Exception):
    """Raised when the catalog cannot satisfy a read because of a
    persistence-layer problem (DB unavailable, schema mismatch, etc.).
    The route layer maps this to HTTP 503 with `retryable: true`.
    """

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class CatalogTierMissing(Exception):
    """Raised when a template has neither a template-level catalog entry
    nor a family-level fallback for any tier. The route layer maps this
    to HTTP 404 with `code: CATALOG_TIER_MISSING` per
    docs/pricing/v2/catalog-tier-contract.md §11.
    """

    def __init__(self, template_id: str):
        super().__init__(f"No catalog entry for template_id={template_id!r}")
        self.template_id = template_id


class CatalogTemplateNotFound(Exception):
    """Raised when an upsert targets a `template_id` that has no row in
    `pricing_template_v2`. The route layer maps to HTTP 404 with
    `code: CATALOG_TEMPLATE_NOT_FOUND`.
    """

    def __init__(self, template_id: str):
        super().__init__(f"Template {template_id!r} is not registered.")
        self.template_id = template_id


class CatalogValidationError(Exception):
    """Base class for save-side validation failures (§4 invariants,
    rounding version whitelist, axis-mode pairing). Each subclass
    carries the structured details needed for the 422 envelope.

    The route layer maps subclasses to specific `code:` values per
    docs/pricing/v2/catalog-tier-contract.md §11.
    """

    code: str = "CATALOG_VALIDATION_ERROR"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class CatalogProBelowFlash(CatalogValidationError):
    """§4.2 violation — submitted Pro tier's `manual_price` is below the
    same template's Flash tier (after merging with latest-enabled rows
    not in the submission)."""

    code = "CATALOG_PRO_BELOW_FLASH"


class CatalogProSurchargeMismatch(CatalogValidationError):
    """§4.1 invariant violation — `pro.manual_price !=
    flash.manual_price + pro.pro_surcharge_display` for the merged set
    of Flash/Pro tiers at upsert time."""

    code = "CATALOG_PRO_SURCHARGE_MISMATCH"


class CatalogProNeedsFlash(CatalogValidationError):
    """The implementation requirement — a Pro tier was submitted without `pro_surcharge_display`
    and no matching Flash tier exists in either the submission or the
    existing catalog to derive it from. Distinct from
    `CATALOG_PRO_SURCHARGE_MISMATCH` so the client can tell "I'm
    missing a sibling Flash" from "my supplied surcharge doesn't add
    up". The route layer maps this to 422 with
    `code: CATALOG_PRO_NEEDS_FLASH`."""

    code = "CATALOG_PRO_NEEDS_FLASH"


class CatalogRoundingVersionUnknown(CatalogValidationError):
    """Caller supplied a `rounding_rule_version` that isn't in the
    server-side whitelist. Adding a new version is a deliberate
    deploy-time decision, not a per-row override."""

    code = "CATALOG_ROUNDING_VERSION_UNKNOWN"


class CatalogFlashProAxisViolation(CatalogValidationError):
    """`flash_pro_axis = 'optional'` paired with non-NULL `mode` or
    `quality`. The DDL CHECK would catch this too; pre-validating in
    the route layer gives a clean 422 envelope instead of relying on
    IntegrityError → 503 fallback."""

    code = "CATALOG_FLASH_PRO_AXIS_VIOLATION"


class CatalogInheritedInvariantViolation(Exception):
    """Raised when family-inheritance arithmetic produces a Pro/Flash
    pair that violates the catalog contract §4.1 invariant
    (`pro.manual_price = flash.manual_price + pro.pro_surcharge_display`)
    *after* multiplier × rounding is applied.

    Distinct from `CATALOG_PRO_SURCHARGE_MISMATCH` (which fires on
    operator upsert): this one only surfaces on read, because rounding
    drift can only be detected once the effective tiers are derived.
    The route layer maps this to HTTP 422 with
    `code: CATALOG_INHERITED_INVARIANT_VIOLATION`.

    Per security hardening: caller needs to distinguish "operator
    typed wrong values" (already enforced at upsert) vs "family base ×
    multiplier inherited badly" (only surfaces on read).
    """

    def __init__(
        self,
        *,
        template_id: str,
        product_line: str,
        mode: str | None,
        flash_manual_price: int,
        pro_manual_price: int,
        inherited_surcharge: int | None,
        derived_surcharge: int,
    ):
        super().__init__(
            f"Family inheritance produced inconsistent Pro/Flash pair for "
            f"template_id={template_id!r}, product_line={product_line!r}, mode={mode!r}: "
            f"pro_manual_price ({pro_manual_price}) - flash_manual_price ({flash_manual_price}) "
            f"= {derived_surcharge} but inherited surcharge is {inherited_surcharge}."
        )
        self.template_id = template_id
        self.product_line = product_line
        self.mode = mode
        self.flash_manual_price = flash_manual_price
        self.pro_manual_price = pro_manual_price
        self.inherited_surcharge = inherited_surcharge
        self.derived_surcharge = derived_surcharge
