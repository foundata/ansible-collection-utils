# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recorded command calls and adversarial boundary cases, without a product."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from collections.abc import Mapping
from dataclasses import replace
from subprocess import Popen
from typing import Any, NoReturn, cast
from unittest.mock import patch

import pytest
from ansible.module_utils import basic

from ansible_collections.foundata.utils.plugins.module_utils import exec as runner
from ansible_collections.foundata.utils.plugins.module_utils.deadline import Deadline
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)
from ansible_collections.foundata.utils.plugins.module_utils.exec import (
    CommandResult,
    CommandTimeout,
    Execution,
    LocatorProfile,
    TimeoutProfile,
    exec_argument_spec,
    run_bounded,
)

SECRET = "SYNTHETIC-EXEC-SECRET-cd15"


class ScriptedProcess:
    """A reaped test child with real pipes for the production callback to drain."""

    def __init__(self, result: CommandResult, stdin: bytes | None = None) -> None:
        self.returncode = result.rc
        self.waits: list[float] = []
        self.stdout = self.output_pipe(result.stdout)
        self.stderr = self.output_pipe(result.stderr)
        self.stdin = None
        self.input_reader = None
        if stdin is not None:
            read_fd, write_fd = os.pipe()
            self.input_reader = os.fdopen(read_fd, "rb")
            self.stdin = os.fdopen(write_fd, "wb")

    @staticmethod
    def output_pipe(data: bytes) -> Any:
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else b"")
        return os.fdopen(read_fd, "rb")

    def poll(self) -> int:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        self.waits.append(timeout)
        return self.returncode


class FakeModule:
    """Scripted transport, including supervisor metadata for interrupted calls."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.run_command_environ_update: dict[str, str] = {}

    def run_command(self, args: list[str], **kwargs: Any) -> tuple[int, bytes, bytes]:
        self.calls.append((args, kwargs))
        step = self.steps.pop(0)
        if callable(step):
            return step(kwargs["before_communicate_callback"])
        if isinstance(step, Exception):
            raise step
        expected: Any = (
            step if isinstance(step, CommandResult) else CommandResult(*step)
        )
        process = ScriptedProcess(expected, kwargs["data"])
        supervisor = kwargs["before_communicate_callback"]
        try:
            with patch.object(runner._Supervisor, "_signal", return_value=False):
                supervisor(cast(Popen[bytes], process))
        except runner._Completed:
            assert supervisor.result is not None
            # Inject status metadata only after real pipe ownership and completion.
            supervisor.result = replace(
                supervisor.result,
                deadline_expired=expected.deadline_expired,
                cleanup_triggered=expected.cleanup_triggered,
            )
            if not isinstance(expected.stdout, bytes) or not isinstance(
                expected.stderr, bytes
            ):
                supervisor.result = expected
            raise
        finally:
            if process.input_reader is not None:
                process.input_reader.close()
        pytest.fail("The supervisor must transfer completion by exception")


def accept_environment(environment: Mapping[str, str]) -> None:
    """This test profile deliberately permits the test process's environment."""


def execution(**kwargs: Any) -> Execution:
    base = Execution(
        argv=["/usr/bin/product", "--", "$HOME", "${HOME}", "~"],
        cwd="/",
        environment={"LANG": "C"},
        validate_effective_environment=accept_environment,
        host_uid=os.geteuid(),
        target_uid=os.geteuid(),
        identity_argv=["/usr/bin/id", "-u"],
    )
    return replace(base, **kwargs)


def run(
    module: FakeModule, context: Execution | None = None, **kwargs: Any
) -> CommandResult:
    return run_bounded(
        module,
        context or execution(),
        profile=TimeoutProfile(),
        deadline=Deadline(10),
        **kwargs,
    )


def test_exact_flags_bytes_stdin_and_literal_arguments() -> None:
    module = FakeModule((7, b"\xff", SECRET.encode()))
    result = run(module, stdin=b"\0\xffpayload")
    argv, options = module.calls[0]
    assert argv[:5] == ["/usr/bin/timeout", "--kill-after", "1.0", "--", "11"]
    assert argv[5:] == list(execution().argv)
    assert options["data"] == b"\0\xffpayload"
    for key in ("binary_data",):
        assert options[key] is True
    for key in (
        "check_rc",
        "handle_exceptions",
        "use_unsafe_shell",
        "expand_user_and_vars",
        "ignore_invalid_cwd",
    ):
        assert options[key] is False
    assert options["encoding"] is None and options["cwd"] == "/"
    assert "timeout" not in options
    assert result.rc == 7 and result.stdout == b"\xff"
    assert result.stderr == SECRET.encode()
    assert SECRET not in repr(result) + repr(execution())
    assert len(module.calls) == 1


@pytest.mark.parametrize("rc", [0, 1, 124, 137, -9])
def test_exit_status_is_not_expiry(rc: int) -> None:
    result = run(FakeModule((rc, b"", b"")))
    assert result.rc == rc and not result.deadline_expired


def test_tiny_budget_never_disables_gnu_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Deadline, "remaining", lambda self: 0.0000001)
    module = FakeModule((0, b"", b""))
    run(module)
    assert module.calls[0][0][4] == "2"


def test_exhausted_budget_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def expired(self: Deadline) -> float:
        raise TransportTimeout()

    monkeypatch.setattr(Deadline, "remaining", expired)
    module = FakeModule()
    with pytest.raises(TransportTimeout):
        run(module)
    assert not module.calls


@pytest.mark.parametrize("stream", [0, 1])
def test_both_streams_are_checked_before_return(stream: int) -> None:
    output = [b"", b""]
    output[stream] = SECRET.encode() * 100
    module = FakeModule((99, *output))
    with pytest.raises(ApiError) as caught:
        run(module, max_output_bytes=32)
    assert caught.value.code == "output_too_large"
    assert SECRET not in str(caught.value) + repr(caught.value)


def test_output_at_bound_is_accepted() -> None:
    assert run(FakeModule((0, b"123", b"456")), max_output_bytes=3).stdout == b"123"


@pytest.mark.parametrize(
    "result", [(True, b"", b""), (0, "text", b""), (0, b"", "text")]
)
def test_malformed_transport_result(result: Any) -> None:
    with pytest.raises(ApiError) as caught:
        run(FakeModule(result))
    assert caught.value.code == "invalid_command_result"


@pytest.mark.parametrize(
    "error, code",
    [
        (OSError(SECRET), "command_io"),
        (ValueError(SECRET), "command_io"),
        (PermissionError(SECRET), "command_start"),
        (FileNotFoundError(SECRET), "command_start"),
    ],
)
def test_operational_exceptions_are_local(error: Exception, code: str) -> None:
    module = FakeModule(error)
    with pytest.raises(ApiError) as caught:
        run(module)
    assert caught.value.code == code
    assert caught.value.__suppress_context__
    assert SECRET not in str(caught.value)
    assert len(module.calls) == 1


def test_hang_metadata_is_independent_and_never_retried() -> None:
    module = FakeModule(CommandResult(-9, b"", b"", True, True))
    with pytest.raises(CommandTimeout) as caught:
        run(module)
    assert caught.value.result.deadline_expired
    assert caught.value.result.cleanup_triggered
    assert len(module.calls) == 1


def test_descendant_cleanup_is_not_a_success() -> None:
    with pytest.raises(ApiError) as caught:
        run(FakeModule(CommandResult(0, b"", b"", False, True)))
    assert caught.value.code == "descendant_cleanup"


@pytest.mark.parametrize("write", [False, True])
def test_timeout_reports_only_the_requested_write_attempt(write: bool) -> None:
    steps: list[Any] = [(0, str(os.geteuid()).encode(), b"")] if write else []
    steps.append(CommandResult(-9, b"", b"", True, True))
    module = FakeModule(*steps)
    with pytest.raises(CommandTimeout) as caught:
        run(module, write=write)
    assert caught.value.write_attempted is write
    assert len(module.calls) == (2 if write else 1)


def test_identity_probe_timeout_is_not_a_write_attempt() -> None:
    module = FakeModule(CommandResult(-9, b"", b"", True, True))
    with pytest.raises(CommandTimeout) as caught:
        run(module, write=True)
    assert not caught.value.write_attempted
    assert len(module.calls) == 1


def test_write_creation_failure_is_not_an_attempt() -> None:
    module = FakeModule((0, str(os.geteuid()).encode(), b""), FileNotFoundError(SECRET))
    with pytest.raises(runner.CommandError) as caught:
        run(module, write=True)
    assert not caught.value.write_attempted
    assert caught.value.code == "command_start"


@pytest.mark.parametrize("error_type", [OSError, PermissionError])
def test_io_failure_after_start_retains_write_attempt(
    error_type: type[OSError],
) -> None:
    def interrupted(supervisor: runner._Supervisor) -> NoReturn:
        supervisor.started = True
        raise error_type(SECRET)

    module = FakeModule((0, str(os.geteuid()).encode(), b""), interrupted)
    with pytest.raises(runner.CommandError) as caught:
        run(module, write=True)
    assert caught.value.write_attempted
    assert caught.value.code == "command_io"
    assert SECRET not in str(caught.value)


def test_final_cleanup_is_included_in_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leader can exit between poll calls after its streams reached EOF."""

    class FinishedProcess:
        returncode = 0
        stdin = None

        def __init__(self) -> None:
            out_read, out_write = os.pipe()
            err_read, err_write = os.pipe()
            os.close(out_write)
            os.close(err_write)
            self.stdout = os.fdopen(out_read, "rb")
            self.stderr = os.fdopen(err_read, "rb")

        def poll(self) -> int:
            return 0

        def wait(self, *, timeout: float) -> int:
            return 0

    process = FinishedProcess()
    monkeypatch.setattr(
        runner._Supervisor,
        "_signal",
        staticmethod(lambda process, sig: sig == signal.SIGKILL),
    )
    supervisor = runner._Supervisor(Deadline(5), None, 16, 0.1)
    with pytest.raises(runner._Completed):
        supervisor(cast(Popen[bytes], process))
    assert supervisor.result is not None and supervisor.result.cleanup_triggered
    assert process.stdout.closed and process.stderr.closed


def test_effective_environment_includes_both_update_layers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INHERITED", SECRET)
    seen: list[dict[str, str]] = []

    def validate(environment: Mapping[str, str]) -> None:
        seen.append(dict(environment))

    module = FakeModule((0, b"", b""))
    module.run_command_environ_update = {"MODULE": "yes", "LANG": "override"}
    run(module, execution(validate_effective_environment=validate))
    assert seen[0]["INHERITED"] == SECRET
    assert seen[0]["MODULE"] == "yes" and seen[0]["LANG"] == "C"
    assert module.calls[0][1]["environ_update"] == seen[0]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/tmp/ansible_modlib.zip:/allowed:/tmp/debug_dir", "/allowed"),
        (
            "/tmp/ansible_modlib.zip::/tmp/debug_dir",
            "/tmp/ansible_modlib.zip::/tmp/debug_dir",
        ),
        ("", ""),
    ],
)
def test_effective_pythonpath_matches_core(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("PYTHONPATH", value)
    module = FakeModule((0, b"", b""))
    run(module)
    assert module.calls[0][1]["environ_update"]["PYTHONPATH"] == expected


def test_disallowed_inherited_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INJECTION", SECRET)

    def reject(environment: Mapping[str, str]) -> None:
        if "INJECTION" in environment:
            raise ValueError(SECRET)

    module = FakeModule()
    with pytest.raises(ApiError) as caught:
        run(module, execution(validate_effective_environment=reject))
    assert caught.value.code == "invalid_environment" and not module.calls
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": []},
        {"argv": "/bin/product"},
        {"argv": ["relative"]},
        {"argv": ["/bin/x", "\0"]},
        {"cwd": "/not/a/real/directory"},
        {"cwd": "~"},
        {"host_uid": True},
        {"target_uid": -1},
    ],
)
def test_invalid_execution_never_starts(overrides: dict[str, Any]) -> None:
    module = FakeModule()
    with pytest.raises(ApiError) as caught:
        run(module, execution(**overrides))
    assert caught.value.code == "invalid_execution" and not module.calls


@pytest.mark.parametrize("environment", [{"BAD=KEY": "x"}, {"KEY": "\0"}, {"": "x"}])
def test_invalid_environment(environment: dict[str, str]) -> None:
    with pytest.raises(ApiError) as caught:
        run(FakeModule(), execution(environment=environment))
    assert caught.value.code == "invalid_environment"


def test_host_and_application_identity_before_write() -> None:
    module = FakeModule((0, str(os.geteuid()).encode() + b"\n", b""), (0, b"done", b""))
    assert run(module, write=True).stdout == b"done"
    assert module.calls[0][0][5:] == ["/usr/bin/id", "-u"]
    assert module.calls[1][0][5:] == list(execution().argv)


def test_wrong_host_identity_prevents_even_the_target_probe() -> None:
    module = FakeModule()
    with pytest.raises(ApiError) as caught:
        run(module, execution(host_uid=os.geteuid() + 1), write=True)
    assert caught.value.code == "host_identity" and not module.calls


@pytest.mark.parametrize(
    "identity", [(0, b"wrong", b""), (1, b"0", b""), (0, b"-1", b"")]
)
def test_wrong_target_identity_prevents_write(identity: Any) -> None:
    module = FakeModule(identity)
    with pytest.raises(ApiError) as caught:
        run(module, write=True)
    assert caught.value.code == "target_identity" and len(module.calls) == 1


@pytest.mark.parametrize("kind", ["package", "podman", "custom"])
def test_profile_requires_records_and_rejects_unused_fields(kind: str) -> None:
    profile = LocatorProfile(
        kind,
        frozenset({"user"}),
        lambda locator, command: execution(argv=command),
        dict.fromkeys(
            ("deadline", "cleanup", "identity", "environment"),
            "test-only qualification",
        ),
    )
    context = profile.resolve({"kind": kind, "user": "test"}, ["/bin/product"])
    assert context.argv == ["/bin/product"]
    with pytest.raises(ApiError) as caught:
        profile.resolve({"kind": kind, "container": "unused"}, [])
    assert caught.value.code == "invalid_locator"
    with pytest.raises(ValueError):
        replace(profile, qualification={})


@pytest.mark.parametrize(
    "options",
    [
        {"kill_after": 0},
        {"kill_after": float("inf")},
        {"executable": "timeout"},
        {"implementation": "busybox"},
    ],
)
def test_unqualified_timeout_is_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TimeoutProfile(**options)


def test_argument_spec_is_deep_fresh() -> None:
    first = exec_argument_spec()
    first["exec"]["options"]["kind"]["choices"].clear()
    assert exec_argument_spec()["exec"]["options"]["kind"]["choices"] == [
        "package",
        "podman",
        "custom",
    ]
    assert exec_argument_spec()["exec"]["options"]["env"]["no_log"] is True


@pytest.mark.parametrize(
    "prefix",
    [
        ["/usr/bin/podman", "exec", "--interactive", "--user", "www-data", "--", "web"],
        ["/usr/bin/zammad", "run"],
    ],
)
def test_prepared_prefix_is_shared_by_target_probe_and_command(
    prefix: list[str],
) -> None:
    """These are scripted channels, not qualification of the named products."""
    context = execution(
        argv=[*prefix, "/product/command", "--", "literal"],
        identity_argv=[*prefix, "/usr/bin/id", "-u"],
        target_uid=2001,
    )
    module = FakeModule((0, b"2001\n", b""), (0, b"", b""))
    run(module, context, write=True)
    assert module.calls[0][0][5:] == [*prefix, "/usr/bin/id", "-u"]
    assert module.calls[1][0][5:] == [*prefix, "/product/command", "--", "literal"]


def test_omitted_suboptions_from_ansible_are_not_unused_fields() -> None:
    profile = LocatorProfile(
        "package",
        frozenset({"user"}),
        lambda locator, command: execution(argv=command),
        dict.fromkeys(("deadline", "cleanup", "identity", "environment"), "test"),
    )
    assert profile.resolve(
        {"kind": "package", "user": "root", "container": None}, ["/bin/id"]
    ).argv == ["/bin/id"]
    with pytest.raises(ApiError):
        profile.resolve({"kind": "package", "container": ""}, ["/bin/id"])


@pytest.mark.parametrize("stdin, exception", [(b"", ValueError), ("text", TypeError)])
def test_stdin_contract(stdin: Any, exception: type[Exception]) -> None:
    with pytest.raises(exception):
        run(FakeModule(), stdin=stdin)


@pytest.mark.parametrize(
    "failure",
    [
        "pipes",
        "selector",
        "register",
        "signal",
        "stdout",
        "stderr",
        "stdin",
        "selector_close",
        "wait",
    ],
)
def test_cleanup_attempts_every_step_after_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    events: list[str] = []
    process = ScriptedProcess(CommandResult(0, b"", b""), b"input")
    original_stdout = process.stdout
    original_stdout_close = original_stdout.close
    selector = runner.selectors.DefaultSelector()

    def fail_step(name: str) -> None:
        events.append(name)
        if failure == name:
            raise OSError(SECRET)

    def make_selector() -> Any:
        fail_step("selector")
        return selector

    def register(*args: Any, **kwargs: Any) -> Any:
        fail_step("register")
        return original_register(*args, **kwargs)

    def signal_process(*args: Any) -> bool:
        fail_step("signal")
        return False

    original_register = selector.register
    monkeypatch.setattr(selector, "register", register)
    original_selector_close = selector.close

    def selector_close() -> None:
        original_selector_close()
        fail_step("selector_close")

    monkeypatch.setattr(selector, "close", selector_close)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", make_selector)
    monkeypatch.setattr(runner._Supervisor, "_signal", staticmethod(signal_process))
    for name in ("stdout", "stderr", "stdin"):
        pipe = getattr(process, name)
        close = pipe.close

        def close_pipe(name: str = name, close: Any = close) -> None:
            close()
            fail_step(name)

        monkeypatch.setattr(pipe, "close", close_pipe)

    def wait(*, timeout: float) -> int:
        assert timeout == 1.0
        fail_step("wait")
        return 0

    monkeypatch.setattr(process, "wait", wait)
    if failure == "pipes":
        process.stdout = None

    def supervise(supervisor: runner._Supervisor) -> None:
        supervisor(cast(Popen[bytes], process))

    module = FakeModule((0, str(os.geteuid()).encode(), b""), supervise)
    # Faults apply only to the requested write, not its identity probe.
    module.steps[0] = complete_identity
    try:
        with pytest.raises(runner.CommandError) as caught:
            run(module, write=True, stdin=b"input")
        assert caught.value.code == "command_io"
        assert caught.value.write_attempted
        assert SECRET not in str(caught.value)
        assert {"signal", "stderr", "stdin", "wait"} <= set(events)
        if failure != "pipes":
            assert "stdout" in events
        if failure not in {"pipes", "selector"}:
            assert "selector_close" in events
        assert (
            process.stderr.closed and process.stdin is not None and process.stdin.closed
        )
    finally:
        original_stdout_close()
        original_selector_close()
        assert process.input_reader is not None
        process.input_reader.close()


def complete_identity(supervisor: runner._Supervisor) -> NoReturn:
    """A preflight-only fixture; fault tests exercise the next callback itself."""
    process = ScriptedProcess(CommandResult(0, str(os.geteuid()).encode(), b""))
    with (
        patch.object(
            runner.selectors, "DefaultSelector", runner.selectors.SelectSelector
        ),
        patch.object(runner._Supervisor, "_signal", return_value=False),
    ):
        supervisor(cast(Popen[bytes], process))
    pytest.fail("The identity callback must transfer completion by exception")


@pytest.mark.parametrize("swallow", [False, True])
def test_missing_completion_signal_is_refused(swallow: bool) -> None:
    def bypass(supervisor: runner._Supervisor) -> tuple[int, bytes, bytes]:
        if swallow:
            try:
                complete_identity(supervisor)
            except runner._Completed:
                pass
        return (0, b"", b"")

    with pytest.raises(runner.CommandError) as caught:
        run(FakeModule(bypass))
    assert caught.value.code == "invalid_command_result"


def real_module(monkeypatch: pytest.MonkeyPatch) -> basic.AnsibleModule:
    monkeypatch.setattr(
        basic,
        "_ANSIBLE_ARGS",
        json.dumps(
            {
                "ANSIBLE_MODULE_ARGS": {
                    "_ansible_remote_tmp": "/tmp",
                    "_ansible_keep_remote_files": False,
                }
            }
        ).encode(),
    )
    if hasattr(basic, "_ANSIBLE_PROFILE"):
        monkeypatch.setattr(basic, "_ANSIBLE_PROFILE", "legacy")
    monkeypatch.setattr(basic.AnsibleModule, "_log_invocation", lambda self: None)
    return basic.AnsibleModule(argument_spec={})


@pytest.mark.parametrize(
    "source, expected",
    [
        ("raise SystemExit(137)", 137),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", -signal.SIGKILL),
    ],
)
def test_real_exit_is_not_observed_expiry(
    monkeypatch: pytest.MonkeyPatch, source: str, expected: int
) -> None:
    module = real_module(monkeypatch)
    timeout = shutil.which("gnutimeout") or shutil.which("timeout")
    assert timeout is not None
    result = run_bounded(
        module,
        execution(argv=[sys.executable, "-c", source]),
        profile=TimeoutProfile(executable=timeout),
        deadline=Deadline(5),
    )
    assert result.rc == expected
    assert not result.deadline_expired and not result.cleanup_triggered


def test_framework_debug_logs_only_loggable_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = real_module(monkeypatch)
    module._debug = True
    logs: list[str] = []
    monkeypatch.setattr(
        module, "log", lambda message, *args, **kwargs: logs.append(message)
    )
    timeout = shutil.which("gnutimeout") or shutil.which("timeout")
    assert timeout is not None
    result = run_bounded(
        module,
        execution(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                "PUBLIC-ARG",
            ]
        ),
        stdin=SECRET.encode(),
        profile=TimeoutProfile(executable=timeout),
        deadline=Deadline(5),
    )
    assert result.stdout == SECRET.encode()
    assert "PUBLIC-ARG" in "".join(logs)
    assert SECRET not in "".join(logs) + repr(result)
