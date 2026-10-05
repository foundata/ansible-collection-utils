# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""Tests of the shared elapsed-time budget."""

from __future__ import annotations

import math
import time

import pytest

from ansible_collections.foundata.utils.plugins.module_utils.deadline import (
    ERROR_CODES,
    Deadline,
)
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


@pytest.mark.parametrize(
    "seconds", [0, -1, 0.0, -0.5, math.inf, -math.inf, math.nan, True, "5", None, b"5"]
)
def test_rejects_non_positive_or_non_finite_budget(seconds: object) -> None:
    with pytest.raises(ApiError) as excinfo:
        Deadline(seconds)  # type: ignore[arg-type]
    assert (excinfo.value.category, excinfo.value.code) == (
        "argument",
        "invalid_deadline",
    )
    assert excinfo.value.code in ERROR_CODES["argument"]


def test_remaining_decreases_with_the_monotonic_clock(clock: FakeClock) -> None:
    deadline = Deadline(10)
    assert deadline.seconds == 10.0
    assert deadline.remaining() == pytest.approx(10.0)
    clock.now += 4
    assert deadline.remaining() == pytest.approx(6.0)
    clock.now += 5.999
    assert deadline.remaining() == pytest.approx(0.001)


def test_exhausted_budget_raises_transport_timeout_every_time(clock: FakeClock) -> None:
    deadline = Deadline(2)
    clock.now += 2
    for _attempt in range(2):
        with pytest.raises(TransportTimeout) as excinfo:
            deadline.remaining()
        assert excinfo.value.category == "timeout"
        assert excinfo.value.code == "deadline_exceeded"
        assert excinfo.value.code in ERROR_CODES["timeout"]
        assert isinstance(excinfo.value, ApiError)


def test_integer_and_float_budgets_are_accepted(clock: FakeClock) -> None:
    assert Deadline(1).remaining() == pytest.approx(1.0)
    assert Deadline(0.25).remaining() == pytest.approx(0.25)


def test_repr_names_the_budget_only() -> None:
    assert repr(Deadline(3)) == "Deadline(seconds=3.0)"
