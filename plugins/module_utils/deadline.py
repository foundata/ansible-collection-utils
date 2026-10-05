# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""One monotonic elapsed-time budget per module operation."""

from __future__ import annotations

import math
import time

from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)

ERROR_CODES: dict[str, frozenset[str]] = {
    "argument": frozenset({"invalid_deadline"}),
    "timeout": frozenset({"deadline_exceeded"}),
}
"""Categories and codes this module raises, for a product's error schema."""


class Deadline:
    """An elapsed-time budget that starts running when it is created.

    Create one per module operation, outside any retry or polling loop, and
    hand the same object to every transport call of that operation. A
    transport never resets it. The budget is cooperative: a transport checks
    it between its own steps and can overrun it by one blocking step. The
    collection README documents those limits.
    """

    __slots__ = ("_expires_at", "_seconds")

    def __init__(self, seconds: float) -> None:
        """Start a budget of ``seconds``.

        Raises:
            ApiError: With category ``argument`` and code ``invalid_deadline``
                unless ``seconds`` is a positive, finite number. The value
                usually comes from a module option, so the module boundary
                reports it like any other expected failure.
        """
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ApiError(
                "The deadline must be a positive, finite number of seconds",
                category="argument",
                code="invalid_deadline",
            )
        self._seconds = float(seconds)
        self._expires_at = time.monotonic() + self._seconds

    @property
    def seconds(self) -> float:
        """The budget this deadline started with."""
        return self._seconds

    def remaining(self) -> float:
        """Return the remaining budget in seconds.

        Raises:
            TransportTimeout: With code ``deadline_exceeded`` once the budget
                is exhausted.
        """
        remaining = self._expires_at - time.monotonic()
        if remaining <= 0:
            raise TransportTimeout(code="deadline_exceeded")
        return remaining

    def __repr__(self) -> str:
        return f"Deadline(seconds={self._seconds!r})"
