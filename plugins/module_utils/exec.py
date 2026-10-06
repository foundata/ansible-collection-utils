# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""Bounded command execution; locator policy and output parsing stay in products.

The synchronous supervisor owns all three pipes in run_command's callback.
It raises a private completion signal to bypass core's unbounded communication
loop. This is deliberately not a timer that kills only the immediate child.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from subprocess import Popen, TimeoutExpired
from typing import Any, Protocol

from ansible_collections.foundata.utils.plugins.module_utils.deadline import Deadline
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    TransportTimeout,
)

ERROR_CODES: dict[str, frozenset[str]] = {
    "argument": frozenset(
        {"invalid_execution", "invalid_locator", "invalid_environment"}
    ),
    "identity": frozenset({"host_identity", "target_identity"}),
    "response": frozenset({"output_too_large", "invalid_command_result"}),
    "transport": frozenset({"command_start", "command_io", "descendant_cleanup"}),
    "timeout": frozenset({"deadline_exceeded"}),
}
"""Local categories and codes; nonzero product exits are returned for parsing."""

DEFAULT_DEADLINE_SECONDS = 120
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
_FIELDS = frozenset(
    {
        "kind",
        "user",
        "container",
        "target_user",
        "executable",
        "argv",
        "chdir",
        "env",
        "target_chdir",
        "target_env",
    }
)


class RunsCommand(Protocol):
    """The subset of AnsibleModule used by the runner."""

    run_command_environ_update: dict[str, str]

    def run_command(
        self,
        args: list[str],
        *,
        data: bytes | None,
        binary_data: bool,
        encoding: None,
        check_rc: bool,
        handle_exceptions: bool,
        use_unsafe_shell: bool,
        expand_user_and_vars: bool,
        cwd: str,
        ignore_invalid_cwd: bool,
        environ_update: dict[str, str],
        before_communicate_callback: Callable[[Popen[bytes]], None],
    ) -> tuple[int, bytes, bytes]:
        """Run an argument list without decoding its streams."""


@dataclass(frozen=True)
class Execution:
    """Product-validated command and its execution and application identities.

    ``identity_argv`` must probe the application's effective numeric UID through
    the same container/wrapper and user options as ``argv``. It prints one
    decimal UID. A product validates paths, identifiers, prefixes and option
    boundaries before constructing this object. All fields can carry sensitive
    data and are omitted from repr. Secrets belong only in run_bounded's stdin.
    """

    argv: Sequence[str] = field(repr=False)
    cwd: str = field(repr=False)
    environment: Mapping[str, str] = field(repr=False)
    validate_effective_environment: Callable[[Mapping[str, str]], None] = field(
        repr=False
    )
    host_uid: int = field(repr=False)
    target_uid: int = field(repr=False)
    identity_argv: Sequence[str] = field(repr=False)


@dataclass(frozen=True)
class TimeoutProfile:
    """Qualified GNU coreutils timeout prerequisite on the execution host.

    The selector enforces the shared deadline. GNU timeout is a fallback at
    ceil(remaining) + 1 seconds, with a separate kill grace. The extra second
    keeps its ambiguous exit statuses out of the normal expiry channel.
    Cleanup covers the timeout process group, including ordinary descendants;
    detached or remote/container work needs product-specific qualification.
    """

    executable: str = "/usr/bin/timeout"
    kill_after: float = 1.0
    implementation: str = "gnu-coreutils"

    def __post_init__(self) -> None:
        if (
            self.implementation != "gnu-coreutils"
            or not _absolute(self.executable)
            or isinstance(self.kill_after, bool)
            or not isinstance(self.kill_after, (int, float))
            or not math.isfinite(self.kill_after)
            or self.kill_after <= 0
        ):
            raise ValueError(
                "A qualified GNU timeout and positive kill grace are required"
            )


@dataclass(frozen=True)
class LocatorProfile:
    """A product's declared locator form, not a claim of product qualification.

    Kinds: package (local binary), podman (container engine), custom (qualified
    wrapper, including a documented application-user transition). ``prepare``
    validates values and builds the product's command and identity probe.
    ``fields`` rejects unused fields before prepare is called. Qualification
    records name the exact deadline, cleanup and identity evidence and limits.
    """

    kind: str
    fields: frozenset[str]
    prepare: Callable[[Mapping[str, Any], Sequence[str]], Execution] = field(repr=False)
    qualification: Mapping[str, str]

    def __post_init__(self) -> None:
        if (
            self.kind not in {"package", "podman", "custom"}
            or not self.fields <= _FIELDS
        ):
            raise ValueError("Invalid locator profile declaration")
        if set(self.qualification) != {
            "deadline",
            "cleanup",
            "identity",
            "environment",
        } or not all(self.qualification.values()):
            raise ValueError(
                "A locator needs deadline, cleanup, identity and environment records"
            )

    def resolve(self, locator: Mapping[str, Any], command: Sequence[str]) -> Execution:
        """Reject unsupported fields, then apply the product's value policy."""
        supplied = {key for key, value in locator.items() if value is not None}
        if locator.get("kind") != self.kind or supplied - self.fields - {"kind"}:
            raise ApiError(
                "Unsupported locator fields",
                category="argument",
                code="invalid_locator",
            )
        return self.prepare(locator, command)


@dataclass(frozen=True)
class CommandResult:
    """Internal bytes and safe status metadata, never an Ansible result.

    Nonzero exits are normal results: a product parser validates the status and
    output structure and derives local diagnostic codes. Neither stream is in
    repr. Deadline/cleanup failures carry this same structure on their error.
    """

    rc: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)
    deadline_expired: bool = False
    cleanup_triggered: bool = False


class CommandError(ApiError):
    """A mechanical failure, with bounded internal output when available."""

    def __init__(
        self,
        *,
        category: str,
        code: str,
        result: CommandResult | None = None,
        write_attempted: bool = False,
    ) -> None:
        super().__init__("Command execution failed", category=category, code=code)
        self.result = result
        self.write_attempted = write_attempted


class CommandTimeout(TransportTimeout):
    """Observed expiry, independent of the command's return code."""

    def __init__(self, result: CommandResult, *, write_attempted: bool = False) -> None:
        super().__init__()
        self.result = result
        self.write_attempted = write_attempted


class _Completed(Exception):
    """Leave run_command before it enters its own pipe handling."""


def _absolute(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and "\0" not in value


def _validate(execution: Execution, module: RunsCommand) -> dict[str, str]:
    if (
        not execution.argv
        or isinstance(execution.argv, (str, bytes))
        or not _absolute(execution.argv[0])
        or any(not isinstance(arg, str) or "\0" in arg for arg in execution.argv)
        or not _absolute(execution.cwd)
        or not os.path.isdir(execution.cwd)
        or any(
            isinstance(uid, bool) or not isinstance(uid, int) or uid < 0
            for uid in (execution.host_uid, execution.target_uid)
        )
    ):
        raise CommandError(category="argument", code="invalid_execution")
    environment = os.environ.copy()
    environment.update(module.run_command_environ_update or {})
    environment.update(execution.environment)
    if any(
        not isinstance(k, str)
        or not k
        or "=" in k
        or "\0" in k
        or not isinstance(v, str)
        or "\0" in v
        for k, v in environment.items()
    ):
        raise CommandError(category="argument", code="invalid_environment")
    # Mirror core's only environment rewrite, after both update layers.
    if "PYTHONPATH" in environment:
        paths = [
            p
            for p in environment["PYTHONPATH"].split(":")
            if p and not p.endswith(("/ansible_modlib.zip", "/debug_dir"))
        ]
        if paths:
            environment["PYTHONPATH"] = ":".join(paths)
    try:
        execution.validate_effective_environment(environment)
    except (ValueError, ApiError):
        raise CommandError(category="argument", code="invalid_environment") from None
    return environment


class _Supervisor:
    """Nonblocking writes, bounded reads, and process-group cleanup on POSIX."""

    def __init__(
        self, deadline: Deadline, stdin: bytes | None, limit: int, grace: float
    ) -> None:
        self.deadline = deadline
        self.stdin = stdin
        self.limit = limit
        self.grace = grace
        self.result: CommandResult | None = None
        self.code: str | None = None
        self.started = False

    @staticmethod
    def _signal(process: Popen[bytes], sig: int) -> bool:
        try:
            os.killpg(process.pid, sig)
            return True
        except ProcessLookupError:
            # GNU timeout may not have created its group yet on early failure.
            if process.poll() is None:
                process.send_signal(sig)
                return True
            return False

    def __call__(self, process: Popen[bytes]) -> None:
        self.started = True
        output = [bytearray(), bytearray()]
        expired = False
        cleanup = False
        stopping: float | None = None
        sent = 0
        selector: selectors.BaseSelector | None = None
        cleanup_failed = False
        try:
            stdout, stderr = process.stdout, process.stderr
            if stdout is None or stderr is None:
                raise ValueError("The supervisor requires piped output")
            streams = (stdout, stderr)
            selector = selectors.DefaultSelector()
            for index, stream in enumerate(streams):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if process.stdin is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 2)
            while True:
                now = time.monotonic()
                if stopping is None:
                    try:
                        remaining = self.deadline.remaining()
                    except TransportTimeout:
                        expired = True
                        remaining = 0.0
                    if expired or self.code or process.poll() is not None:
                        cleanup = self._signal(process, signal.SIGTERM)
                        stopping = now + self.grace
                if stopping is not None:
                    remaining = stopping - now
                    if remaining <= 0:
                        cleanup = bool(selector.get_map()) or cleanup
                        cleanup = self._signal(process, signal.SIGKILL) or cleanup
                        break
                if process.poll() is not None and not selector.get_map():
                    break
                for key, _event in selector.select(min(0.05, max(0.0, remaining))):
                    fd = key.fd
                    index = key.data
                    if index == 2:
                        try:
                            if self.stdin is None:
                                raise ValueError("The supervisor requires byte input")
                            sent += os.write(fd, self.stdin[sent : sent + 4096])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            if not expired:
                                self.code = self.code or "command_io"
                            sent = len(self.stdin or b"")
                        if sent == len(self.stdin or b""):
                            selector.unregister(key.fileobj)
                            if process.stdin is not None:
                                process.stdin.close()
                    else:
                        try:
                            chunk = os.read(
                                fd, min(65536, self.limit - len(output[index]) + 1)
                            )
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif len(output[index]) + len(chunk) > self.limit:
                            self.code = "output_too_large"
                            # Stop reading; keep only the bounded bytes read so far.
                            selector.unregister(key.fileobj)
                        else:
                            output[index].extend(chunk)
            process.wait(timeout=self.grace)
            if self.stdin is not None and sent < len(self.stdin) and not expired:
                self.code = self.code or "command_io"
        finally:
            # Every cleanup operation gets its own attempt, even after setup fails.
            try:
                cleanup = self._signal(process, signal.SIGKILL) or cleanup
            except (OSError, ValueError):
                cleanup_failed = True
            for pipe in (process.stdout, process.stderr, process.stdin):
                if pipe is not None:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        cleanup_failed = True
            if selector is not None:
                try:
                    selector.close()
                except (OSError, ValueError):
                    cleanup_failed = True
            try:
                process.wait(timeout=self.grace)
            except (OSError, ValueError, TimeoutExpired):
                cleanup_failed = True
            if cleanup_failed:
                raise OSError("Command cleanup failed") from None
        self.result = CommandResult(
            process.returncode, bytes(output[0]), bytes(output[1]), expired, cleanup
        )
        raise _Completed


def run_bounded(
    module: RunsCommand,
    execution: Execution,
    *,
    profile: TimeoutProfile,
    deadline: Deadline,
    stdin: bytes | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    write: bool = False,
) -> CommandResult:
    """Run once, without retries or publishing output.

    ``write=True`` verifies both identities before starting the command. The
    product still tracks writes across parsing and read-back, and reports an
    interrupted write as changed=True/outcome=unknown through fail_from.
    Empty stdin is refused because core does not open a pipe for b"".
    Nonzero statuses, including 124/137, are returned to the product parser.
    """
    if stdin is not None and not isinstance(stdin, bytes):
        raise TypeError("stdin must be bytes or None")
    if stdin == b"":
        raise ValueError("Empty stdin does not open a pipe in run_command")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer")
    environment = _validate(execution, module)
    if write:
        if os.geteuid() != execution.host_uid:
            raise CommandError(category="identity", code="host_identity")
        identity = run_bounded(
            module,
            replace(execution, argv=execution.identity_argv),
            profile=profile,
            deadline=deadline,
            max_output_bytes=128,
        )
        if identity.rc != 0 or identity.stdout.strip() != str(
            execution.target_uid
        ).encode("ascii"):
            raise CommandError(category="identity", code="target_identity")
    budget = deadline.remaining()
    supervisor = _Supervisor(deadline, stdin, max_output_bytes, profile.kill_after)
    argv = [
        profile.executable,
        "--kill-after",
        str(profile.kill_after),
        "--",
        str(math.ceil(budget) + 1),
        *execution.argv,
    ]
    try:
        module.run_command(
            argv,
            data=stdin,
            binary_data=True,
            encoding=None,
            check_rc=False,
            handle_exceptions=False,
            use_unsafe_shell=False,
            expand_user_and_vars=False,
            cwd=execution.cwd,
            ignore_invalid_cwd=False,
            environ_update=environment,
            before_communicate_callback=supervisor,
        )
    except _Completed:
        pass
    except (FileNotFoundError, PermissionError):
        raise CommandError(
            category="transport",
            code="command_io" if supervisor.started else "command_start",
            write_attempted=write and supervisor.started,
        ) from None
    except (OSError, ValueError, TimeoutExpired):
        raise CommandError(
            category="transport",
            code="command_io",
            write_attempted=write and supervisor.started,
        ) from None
    else:
        raise CommandError(
            category="response",
            code="invalid_command_result",
            write_attempted=write,
        )
    result = supervisor.result
    if result is None:
        raise CommandError(
            category="response",
            code="invalid_command_result",
            write_attempted=write and supervisor.started,
        ) from None
    if (
        isinstance(result.rc, bool)
        or not isinstance(result.rc, int)
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
    ):
        raise CommandError(
            category="response",
            code="invalid_command_result",
            write_attempted=write and supervisor.started,
        )
    if len(result.stdout) > max_output_bytes or len(result.stderr) > max_output_bytes:
        raise CommandError(
            category="response",
            code="output_too_large",
            result=supervisor.result,
            write_attempted=write,
        )
    if supervisor.code:
        raise CommandError(
            category="response"
            if supervisor.code == "output_too_large"
            else "transport",
            code=supervisor.code,
            result=result,
            write_attempted=write,
        )
    if result.deadline_expired:
        raise CommandTimeout(result, write_attempted=write)
    if result.cleanup_triggered:
        raise CommandError(
            category="transport",
            code="descendant_cleanup",
            result=result,
            write_attempted=write,
        )
    return result


def exec_argument_spec() -> dict[str, Any]:
    """Fresh mechanical options; each product narrows the supported profiles."""
    return {
        "deadline": {"type": "int", "default": DEFAULT_DEADLINE_SECONDS},
        "exec": {
            "type": "dict",
            "required": True,
            "options": {
                "kind": {
                    "type": "str",
                    "required": True,
                    "choices": ["package", "podman", "custom"],
                },
                "user": {"type": "str", "required": True},
                "container": {"type": "str"},
                "target_user": {"type": "str", "required": True},
                "executable": {"type": "path"},
                "argv": {"type": "list", "elements": "str"},
                "chdir": {"type": "path", "default": "/"},
                "env": {"type": "dict", "no_log": True},
                "target_chdir": {"type": "path"},
                "target_env": {"type": "dict", "no_log": True},
            },
        },
    }
