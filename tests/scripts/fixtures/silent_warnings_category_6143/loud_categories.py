"""Fixture for test_silent_warnings_category_gate_6143.py -- the
false-reject-side witness. Every call here uses a category Python does
NOT ignore by default (UserWarning, explicit or omitted), or a real
`logger.warning` -- none of these should ever be flagged.
"""
from __future__ import annotations

import logging
import warnings


def explicit_user_warning() -> None:
    warnings.warn("visible by default", UserWarning, stacklevel=2)


def omitted_category_defaults_to_user_warning() -> None:
    warnings.warn("visible by default, category omitted", stacklevel=2)


def already_promoted_to_logger() -> None:
    logging.getLogger(__name__).warning("this reaches reyn.log / stderr")
