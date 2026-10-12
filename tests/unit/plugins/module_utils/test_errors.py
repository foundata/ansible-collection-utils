# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the shared error schema, the catch boundary and safe failure."""

from __future__ import annotations

import json
import traceback
from typing import Any, NoReturn

import pytest
from ansible.module_utils import basic
from ansible.module_utils.common.text.converters import to_bytes

from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    OUTCOMES,
    ApiError,
    ErrorSchema,
    TransportTimeout,
    fail_from,
    merge_error_codes,
)

SECRET = "SYNTHETIC-SECRET-0f3a9c21"
# Payload-only Python lanes have no ansible.release; the traceback machinery
# that 2.19 introduced is the feature under test, so detect it directly.
CORE_CAPTURES_TRACEBACKS = hasattr(basic, "_traceback")


class ProductError(ApiError):
    """A product's own failure type, as the guide prescribes."""


class RecordingModule:
    """Records the fail_json call instead of exiting."""

    def __init__(self) -> None:
        self.result: dict[str, Any] | None = None

    def fail_json(self, msg: str, **kwargs: Any) -> NoReturn:
        self.result = {"msg": msg, **kwargs}
        raise SystemExit(1)


FALLBACK = ("internal", None)
VALIDATED = ("product", "validation_failed")


def schema(**validators: Any) -> ErrorSchema:
    return ErrorSchema(
        codes_by_category={
            "internal": {None},
            "product": {"validation_failed", "not_found"},
            "timeout": {"deadline_exceeded", "socket_timeout"},
        },
        fallback=("internal", None),
        field_validators=validators,
    )


def fail(module: RecordingModule, error: ApiError, **kwargs: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "schema": schema(**kwargs.pop("validators", {})),
        "msg": "Operation failed locally",
        "changed": False,
        "outcome": "unchanged",
    }
    arguments.update(kwargs)
    with pytest.raises(SystemExit):
        fail_from(module, error, **arguments)
    assert module.result is not None
    return module.result


def test_api_error_defaults_and_attributes() -> None:
    error = ApiError()
    assert (str(error), error.category, error.code, error.status) == (
        "Operation failed",
        "internal",
        None,
        None,
    )
    error = ApiError("boom", category="product", code="not_found", status=404)
    assert (error.category, error.code, error.status) == ("product", "not_found", 404)


@pytest.mark.parametrize("explicit_cause", [False, True])
def test_validator_exception_cannot_chain_the_handled_error(
    explicit_cause: bool,
) -> None:
    module = RecordingModule()
    failure = KeyError("x")
    cause = ApiError(SECRET)
    with pytest.raises(KeyError) as caught:
        try:
            raise cause
        except ApiError as handled:

            def validator(value: object) -> NoReturn:
                if explicit_cause:
                    raise failure from cause
                raise failure

            fail_from(
                module,
                handled,
                schema=schema(resource=validator),
                msg="Local failure",
                changed=False,
                outcome="unchanged",
                fields={"resource": "x"},
            )

    assert caught.value is failure
    assert failure.args == ("x",)
    assert failure.__context__ is None
    assert failure.__cause__ is None
    assert SECRET not in "".join(
        traceback.format_exception(type(failure), failure, failure.__traceback__)
    )
    assert module.result is None


def test_transport_timeout_is_a_timeout_api_error() -> None:
    error = TransportTimeout()
    assert isinstance(error, ApiError)
    assert (error.category, error.code) == ("timeout", "deadline_exceeded")
    assert TransportTimeout(code="socket_timeout").code == "socket_timeout"


def test_shared_base_is_the_catch_boundary() -> None:
    def raise_timeout() -> None:
        raise TransportTimeout()

    # A product subclass does not catch the sibling timeout type.
    with pytest.raises(TransportTimeout):
        try:
            raise_timeout()
        except ProductError:
            pytest.fail("ProductError must not catch TransportTimeout")
    # The shared base does, and the narrower handler can still come first.
    seen: list[str] = []
    try:
        raise_timeout()
    except TransportTimeout:
        seen.append("timeout")
    except ApiError:
        seen.append("base")
    try:
        raise ProductError("x")
    except ApiError:
        seen.append("base")
    assert seen == ["timeout", "base"]


def test_schema_rejects_a_fallback_it_does_not_allow() -> None:
    with pytest.raises(ValueError, match="fallback"):
        ErrorSchema(codes_by_category={"internal": {None}}, fallback=("product", "x"))
    with pytest.raises(ValueError, match="fallback"):
        ErrorSchema(codes_by_category={"internal": {None}}, fallback=("internal", "x"))


def test_schema_allows_only_known_pairs() -> None:
    allowed = schema()
    assert allowed.allows("product", "not_found")
    assert allowed.allows("internal", None)
    assert not allowed.allows("product", None)
    assert not allowed.allows("product", "unknown")
    assert not allowed.allows("unknown", "not_found")
    assert not allowed.allows(None, None)
    assert not allowed.allows("product", ["not_found"])


def test_merge_error_codes_unions_categories() -> None:
    merged = merge_error_codes(
        {"timeout": {"deadline_exceeded"}, "a": {"x"}},
        {"timeout": {"socket_timeout"}, "b": {None}},
    )
    assert merged == {
        "timeout": frozenset({"deadline_exceeded", "socket_timeout"}),
        "a": frozenset({"x"}),
        "b": frozenset({None}),
    }


def test_known_pair_and_status_pass_through() -> None:
    module = RecordingModule()
    result = fail(
        module, ProductError("raw", category="product", code="not_found", status=404)
    )
    assert result == {
        "msg": "Operation failed locally",
        "error_category": "product",
        "error_code": "not_found",
        "http_status": 404,
        "changed": False,
        "outcome": "unchanged",
        "exception": None,
    }


@pytest.mark.parametrize(
    ("category", "code"),
    [
        ("product", "made_up"),
        ("made_up", "not_found"),
        ("product", None),
        ("internal", "x"),
    ],
)
def test_unknown_pair_uses_the_fallback(category: str, code: str | None) -> None:
    result = fail(RecordingModule(), ApiError("raw", category=category, code=code))
    assert (result["error_category"], result["error_code"]) == ("internal", None)


@pytest.mark.parametrize("status", [99, 600, "404", 4.04, True, None])
def test_invalid_status_is_dropped(status: Any) -> None:
    error = ProductError("raw", category="product", code="not_found")
    error.status = status
    assert fail(RecordingModule(), error)["http_status"] is None


def test_fields_are_validated_and_published() -> None:
    result = fail(
        RecordingModule(),
        ProductError("raw", category="product", code="validation_failed"),
        changed=True,
        outcome="unknown",
        fields={"resource": "web", "attempts": 3},
        validators={"resource": str, "attempts": int},
    )
    assert result["resource"] == "web"
    assert result["attempts"] == 3
    assert (result["changed"], result["outcome"]) == (True, "unknown")


@pytest.mark.parametrize(
    "key",
    [
        "changed",
        "error_category",
        "error_code",
        "exception",
        "failed",
        "http_status",
        "msg",
        "outcome",
        "ansible_facts",
        "deprecations",
        "diff",
        "invocation",
        "rc",
        "results",
        "skipped",
        "warnings",
        "stdout",
        "stderr",
        "stdout_lines",
        "stderr_lines",
        "_ansible_no_log",
        "_ansible_verbose_always",
        "_ansible_new_secrets",
        "_ansible_future_key",
    ],
)
def test_reserved_result_keys_are_dropped(key: str) -> None:
    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="product", code="validation_failed"),
            schema=schema(**{key: str}),
            msg="m",
            changed=False,
            outcome="unchanged",
            fields={key: "x"},
        )
    assert module.result is not None
    assert module.result.get(key) != "x"
    assert (module.result["changed"], module.result["outcome"]) == (False, "unchanged")
    assert (module.result["error_category"], module.result["error_code"]) == VALIDATED


def test_field_without_validator_is_dropped() -> None:
    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="product", code="validation_failed"),
            schema=schema(),
            msg="m",
            changed=True,
            outcome="unknown",
            fields={"resource": "web"},
        )
    assert module.result is not None
    assert "resource" not in module.result
    assert (module.result["changed"], module.result["outcome"]) == (True, "unknown")
    assert (module.result["error_category"], module.result["error_code"]) == VALIDATED


@pytest.mark.parametrize("key", [1, None, ("resource",), b"resource"])
def test_non_str_field_key_is_dropped(key: Any) -> None:
    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="product", code="validation_failed"),
            schema=schema(resource=str),
            msg="m",
            changed=True,
            outcome="unknown",
            fields={key: "web", "resource": "web"},
        )
    assert module.result is not None
    assert module.result["resource"] == "web"
    assert (module.result["changed"], module.result["outcome"]) == (True, "unknown")
    assert (module.result["error_category"], module.result["error_code"]) == VALIDATED


@pytest.mark.parametrize("exception", [ValueError, TypeError])
def test_rejected_value_is_dropped_without_the_value(
    exception: type[Exception],
) -> None:
    def reject(value: object) -> object:
        raise exception(f"bad value {value!r}")

    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="product", code="validation_failed"),
            schema=schema(resource=reject),
            msg="m",
            changed=False,
            outcome="unchanged",
            fields={"resource": SECRET},
        )
    assert module.result is not None
    assert "resource" not in module.result
    assert SECRET not in repr(module.result)
    assert (module.result["error_category"], module.result["error_code"]) == VALIDATED


def test_rejected_identity_keeps_the_argument_code() -> None:
    # The caller's invalid name is the reason for the failure; the validator
    # rightly refuses to publish it, and the code must still say so.
    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="argument", code="invalid_name"),
            schema=ErrorSchema(
                codes_by_category={"argument": {"invalid_name"}, "internal": {None}},
                fallback=("internal", None),
                field_validators={"name": reject_unless_lowercase},
            ),
            msg="m",
            changed=False,
            outcome="unchanged",
            fields={"name": "Web-01"},
        )
    assert module.result is not None
    assert "name" not in module.result
    assert (module.result["error_category"], module.result["error_code"]) == (
        "argument",
        "invalid_name",
    )


def test_unknown_pair_still_falls_back_with_dropped_fields() -> None:
    module = RecordingModule()
    with pytest.raises(SystemExit):
        fail_from(
            module,
            ApiError(category="product", code="unlisted"),
            schema=schema(),
            msg="m",
            changed=False,
            outcome="unchanged",
            fields={"resource": "web"},
        )
    assert module.result is not None
    assert (module.result["error_category"], module.result["error_code"]) == FALLBACK


def reject_unless_lowercase(value: object) -> object:
    if isinstance(value, str) and value.isalpha() and value.islower():
        return value
    raise ValueError("invalid name")


@pytest.mark.parametrize(
    ("changed", "outcome"),
    [
        (False, "unknown"),
        (False, "changed"),
        (True, "unchanged"),
        (True, "done"),
        (False, None),
        (False, []),
        (False, {}),
    ],
)
def test_outcome_must_agree_with_changed(changed: bool, outcome: Any) -> None:
    with pytest.raises(ValueError):
        fail_from(
            RecordingModule(),
            ApiError(),
            schema=schema(),
            msg="m",
            changed=changed,
            outcome=outcome,
        )
    assert OUTCOMES == {"unchanged", "unknown", "changed"}


def test_programming_failures_suppress_handled_error() -> None:
    try:
        try:
            raise ApiError(SECRET)
        except ApiError as error:
            fail_from(
                RecordingModule(),
                error,
                schema=schema(),
                msg="Local",
                changed=False,
                outcome=[],  # type: ignore[arg-type]
            )
    except ValueError:
        assert SECRET not in traceback.format_exc()
    else:
        pytest.fail("A local programming failure was required")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"msg": ""},
        {"msg": "   "},
        {"msg": None},
        {"changed": "yes"},
        {"changed": 1},
    ],
)
def test_invalid_msg_or_changed_is_rejected(kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {
        "schema": schema(),
        "msg": "m",
        "changed": False,
        "outcome": "unchanged",
    }
    arguments.update(kwargs)
    with pytest.raises((TypeError, ValueError)):
        fail_from(RecordingModule(), ApiError(), **arguments)


def test_non_api_error_is_rejected() -> None:
    with pytest.raises(TypeError):
        fail_from(
            RecordingModule(),
            RuntimeError("x"),  # type: ignore[arg-type]
            schema=schema(),
            msg="m",
            changed=False,
            outcome="unchanged",
        )


def run_real_module(
    capsys: pytest.CaptureFixture[str], handler: Any, *, tracebacks: bool
) -> dict[str, Any]:
    """Run ``handler`` inside a real AnsibleModule and return its JSON result."""
    module_args: dict[str, Any] = {
        "password": SECRET,
        "_ansible_remote_tmp": "/tmp",
        "_ansible_keep_remote_files": False,
    }
    if tracebacks:
        # The controller passes this from ANSIBLE_DISPLAY_TRACEBACK from 2.19 on.
        module_args["_ansible_tracebacks_for"] = ["error", "warning", "deprecated"]
    basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": module_args}))
    if hasattr(basic, "_ANSIBLE_PROFILE"):
        # Cores from 2.19 also need the serialization profile of the arguments.
        basic._ANSIBLE_PROFILE = "legacy"
    module = basic.AnsibleModule(
        argument_spec={"password": {"type": "str", "no_log": True}},
        supports_check_mode=True,
    )
    with pytest.raises(SystemExit):
        try:
            raise ProductError(
                f"Login with password {SECRET} rejected",
                category="product",
                code="not_found",
            )
        except ApiError as error:
            handler(module, error)
    output = capsys.readouterr().out
    return json.loads(output)


@pytest.mark.skipif(
    not CORE_CAPTURES_TRACEBACKS, reason="traceback capture exists from 2.19"
)
def test_capture_leaks_the_error_text_without_exception_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control: a handler that omits exception=None leaks on a capturing core."""

    def unsafe(module: basic.AnsibleModule, error: ApiError) -> None:
        module.fail_json(msg="Operation failed locally", error_code=error.code)

    result = run_real_module(capsys, unsafe, tracebacks=True)
    assert "rejected" in json.dumps(result)


@pytest.mark.parametrize("tracebacks", [False, True])
def test_fail_from_keeps_the_secret_and_the_error_text_out(
    capsys: pytest.CaptureFixture[str], tracebacks: bool
) -> None:
    if tracebacks and not CORE_CAPTURES_TRACEBACKS:
        pytest.skip("traceback capture exists from 2.19")

    def safe(module: basic.AnsibleModule, error: ApiError) -> None:
        fail_from(
            module,
            error,
            schema=schema(resource=str),
            msg="Operation failed locally",
            changed=True,
            outcome="unknown",
            fields={"resource": "web"},
        )

    result = run_real_module(capsys, safe, tracebacks=tracebacks)
    # From ansible-core 2.23 the raw result carries the declared no_log values
    # for the controller, which masks them and drops the key before display.
    new_secrets = result.pop("_ansible_new_secrets", None)
    assert new_secrets is None or new_secrets == [SECRET]
    serialized = json.dumps(result)
    assert SECRET not in serialized
    assert "rejected" not in serialized
    assert result["failed"] is True
    assert result["msg"] == "Operation failed locally"
    assert (result["error_category"], result["error_code"]) == ("product", "not_found")
    assert result["changed"] is True
    assert result["outcome"] == "unknown"
    assert result["resource"] == "web"
    assert SECRET not in json.dumps(result.get("invocation", {}))
