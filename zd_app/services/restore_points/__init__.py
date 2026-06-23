"""Restore Points UI-support package — boundary copy + (future) helpers.

Storage + service live next door at
:mod:`zd_app.storage.restore_point_models` /
:mod:`zd_app.storage.restore_point_store` /
:mod:`zd_app.services.restore_point_service`. This package only re-exports
the claim-boundary constants the UI + JSON exporters need.
"""

from __future__ import annotations

from zd_app.services.restore_points.boundary import (
    CLAIM_BOUNDARY_PARAGRAPH,
    CLAIM_BOUNDARY_SHORT_UI,
)


__all__ = [
    "CLAIM_BOUNDARY_PARAGRAPH",
    "CLAIM_BOUNDARY_SHORT_UI",
]
