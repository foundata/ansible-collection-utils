# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared exception base, error schema and safe failure for product modules.

A product collection derives its own failures from :class:`ApiError` and
catches that shared base at the module boundary, where :func:`fail_from`
turns the caught error into ``fail_json`` without publishing the error's text.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

OUTCOMES = frozenset({"unchanged", "unknown", "changed"})
"""Mutation outcomes a failure result may carry, see :func:`fail_from`."""

RESERVED_RESULT_KEYS = frozenset(
    {
        # Set by fail_from itself.
        "changed",
        "error_category",
        "error_code",
        "exception",
        "failed",
        "http_status",
        "msg",
        "outcome",
        # Interpreted by Ansible.
        "ansible_facts",
        "deprecations",
        "diff",
        "invocation",
        "rc",
        "results",
        "skipped",
        "stdout",
        "stderr",
        "stdout_lines",
        "stderr_lines",
        "warnings",
    }
)
"""Exact result keys fail_from leaves out of fields, as it does the _ansible_ prefix."""


class FailsJson(Protocol):
    """What :func:`fail_from` needs from a module: ``AnsibleModule`` fits."""

    def fail_json(self, msg: str, **kwargs: Any) -> NoReturn:
        """Exit the module with a failure result."""


class ApiError(Exception):
    """Base of every failure a product client or a shared transport raises.

    ``category`` and ``code`` are local identifiers that a product's
    :class:`ErrorSchema` allowlists; ``status`` is an HTTP status or ``None``.
    The message is for developers: :func:`fail_from` never publishes it, so
    it may name what went wrong but must still not carry secrets.
    """

    def __init__(
        self,
        message: str = "Operation failed",
        *,
        category: str = "internal",
        code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.status = status


class TransportTimeout(ApiError):
    """A socket timeout or an exhausted elapsed-time budget.

    Always carries the category ``timeout``. A product's own subclass of
    :class:`ApiError` does not catch it; the module boundary catches the
    shared base and a narrower handler may reconcile within the deadline.
    """

    def __init__(self, *, code: str = "deadline_exceeded") -> None:
        super().__init__(f"Transport timed out ({code})", category="timeout", code=code)


@dataclass(frozen=True)
class ErrorSchema:
    """The public error schema of a product collection.

    ``codes_by_category`` maps each allowed category to its allowed codes;
    ``None`` in a collection permits an error without a code. ``fallback`` is
    the category and code pair published for an error the schema does not
    know and must itself be allowed. ``field_validators`` maps each extra
    result key :func:`fail_from` may publish to a callable that returns the
    value to publish or raises ``ValueError`` or ``TypeError``. Validators are
    trusted product code and must allowlist public values, not just coerce them.
    """

    codes_by_category: Mapping[str, Collection[str | None]]
    fallback: tuple[str, str | None]
    field_validators: Mapping[str, Callable[[object], object]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.allows(*self.fallback):
            raise ValueError("The fallback pair must be allowed by codes_by_category")

    def allows(self, category: object, code: object) -> bool:
        """Return whether the category and code pair is part of the schema."""
        if not isinstance(category, str) or not isinstance(code, (str, type(None))):
            return False
        codes = self.codes_by_category.get(category)
        return codes is not None and code in codes


def merge_error_codes(
    *mappings: Mapping[str, Collection[str | None]],
) -> dict[str, frozenset[str | None]]:
    """Return the union of several ``codes_by_category`` mappings.

    Shared helpers publish the codes they raise; a product merges them with
    its own before building its :class:`ErrorSchema`.
    """
    merged: dict[str, set[str | None]] = {}
    for mapping in mappings:
        for category, codes in mapping.items():
            merged.setdefault(category, set()).update(codes)
    return {category: frozenset(codes) for category, codes in merged.items()}


def fail_from(
    module: FailsJson,
    error: ApiError,
    *,
    schema: ErrorSchema,
    msg: str,
    changed: bool,
    outcome: str,
    fields: Mapping[str, object] | None = None,
) -> NoReturn:
    """Fail the module from a caught error without publishing its text.

    The result carries ``msg``, the error's category, code and HTTP status
    after validation against ``schema``, the caller's ``changed`` and
    ``outcome``, and every entry of ``fields`` after its validator ran.
    ``exception=None`` keeps the error's message out of a captured traceback.
    The caller supplies ``changed`` and ``outcome`` because it tracks mutation.

    Raises:
        TypeError: If ``error`` is no :class:`ApiError` or ``changed`` is no
            boolean.
        ValueError: If ``msg`` is empty or ``outcome`` is not in
            :data:`OUTCOMES` or disagrees with ``changed``. These are
            programming errors; they are raised from ``None`` so that the
            handled error's text stays out of the chained traceback.

    A field the schema cannot publish never raises: a key that is not a
    string, a reserved key, a key without validator or a value its validator
    rejects with ``ValueError`` or ``TypeError`` is left out of the result.
    Such a field comes from the product or the caller, and a module must
    still end with ``changed`` and ``outcome``. The category and code stay as
    validated; only a pair the schema does not allow becomes the fallback.
    Other validator exceptions propagate with their context and cause cleared.
    Validators must keep their own exception text safe for publication.
    """
    if not isinstance(error, ApiError):
        raise TypeError("error must be an ApiError") from None
    if not isinstance(msg, str) or not msg.strip():
        raise ValueError("msg must be a non-empty local message") from None
    if not isinstance(changed, bool):
        raise TypeError("changed must be a bool") from None
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}") from None
    if (outcome == "unchanged") == changed:
        raise ValueError(
            f"outcome {outcome!r} disagrees with changed={changed!r}"
        ) from None

    category, code = error.category, error.code
    if not schema.allows(category, code):
        category, code = schema.fallback
    status = error.status
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        status = None

    extra: dict[str, object] = {}
    for key, value in (fields or {}).items():
        if (
            not isinstance(key, str)
            or key in RESERVED_RESULT_KEYS
            or key.startswith("_ansible_")
        ):
            continue
        validator = schema.field_validators.get(key)
        if validator is None:
            continue
        try:
            extra[key] = validator(value)
        except (ValueError, TypeError):
            continue
        except Exception as validator_error:
            # Core can inspect the exception chain even when display is suppressed.
            validator_error.__context__ = None
            validator_error.__cause__ = None
            raise

    module.fail_json(
        msg=msg,
        error_category=category,
        error_code=code,
        http_status=status,
        changed=changed,
        outcome=outcome,
        exception=None,
        **extra,
    )
