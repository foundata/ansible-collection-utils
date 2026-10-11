#!/usr/bin/python
# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
---
module: exec_probe
short_description: Exercise bounded commands inside the test container
description: "Test-only consumer, with fixed commands and local diagnostic codes."
version_added: "1.0.0"
author: "foundata Development Team (@foundata)"
extends_documentation_fragment:
  - foundata.utils.exec
options:
  case:
    description: "Fixed test case to run."
    type: str
    required: true
    choices:
      - roundtrip
      - literal
      - environment
      - status
      - sleep
      - stdout
      - stderr
      - descendant
      - descendant_closed
      - stdin_holder
      - short_stdin
      - rejected
      - host_identity
      - target_identity
      - write
  secret:
    description: "Synthetic stdin secret, never published."
    type: str
    required: true
  timeout_executable:
    description: "Absolute GNU timeout path in the test image."
    type: path
    default: "/usr/bin/timeout"
"""

EXAMPLES = r"""
---
- name: "Exercise byte stdin"
  exec_probe:
    case: "roundtrip"
    secret: "test-only"
    exec:
      kind: "package"
      user: "root"
      target_user: "root"
"""

RETURN = r"""
---
checked:
  description: "Whether the fixed assertion passed."
  type: bool
  returned: success
elapsed:
  description: "Elapsed seconds."
  type: float
  returned: always
deadline_expired:
  description: "Whether the supervisor observed expiry."
  type: bool
  returned: failure
cleanup_triggered:
  description: "Whether process-group cleanup was needed."
  type: bool
  returned: failure
descendant_stopped:
  description: "Whether the test descendant is absent or a zombie awaiting its container init."
  type: bool
  returned: failure
"""

import json
import os
import pwd
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.foundata.utils.plugins.module_utils.deadline import Deadline
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    ErrorSchema,
    fail_from,
    merge_error_codes,
)
from ansible_collections.foundata.utils.plugins.module_utils.exec import (
    ERROR_CODES,
    CommandError,
    CommandResult,
    Execution,
    LocatorProfile,
    TimeoutProfile,
    exec_argument_spec,
    run_bounded,
)

SCRIPTS = {
    "roundtrip": "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(data)",
    "literal": "import json,sys; print(json.dumps(sys.argv[1:]))",
    "environment": "import os; print(os.environ['EXEC_PROBE'])",
    "status": "raise SystemExit(124)",
    "stdout": "import os; data=b'x'*8192\nwhile True: os.write(1,data)",
    "stderr": "import os; data=b'x'*8192\nwhile True: os.write(2,data)",
    "descendant": "import subprocess; p=subprocess.Popen(['/bin/sleep','60']); print(p.pid, flush=True)",
    "stdin_holder": "import subprocess,time; p=subprocess.Popen(['/bin/sleep','60']); print(p.pid, flush=True); time.sleep(60)",
    "descendant_closed": (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "print(1,flush=True); os.close(1); os.close(2); time.sleep(60)'],stdout=subprocess.PIPE); "
        "p.stdout.readline(); print(p.pid,flush=True)"
    ),
    "short_stdin": "import sys; sys.stdin.buffer.read(1)",
    "rejected": "import sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(data[::-1] + b'PRODUCT-PRIVATE-c418'); raise SystemExit(1)",
    "write": "pass",
    "host_identity": "pass",
    "target_identity": "pass",
}


def validate_environment(environment: Mapping[str, str]) -> None:
    """Test profile permits the container environment and checks its addition."""
    if environment.get("EXEC_PROBE") != "literal-$HOME":
        raise ValueError("Missing test environment")


def descendant_stopped(result: CommandResult | None, case: str) -> bool:
    if case not in {"descendant", "descendant_closed", "stdin_holder"}:
        return True
    if result is None or not result.stdout.strip().isdigit():
        return False
    pid = int(result.stdout.strip())
    for _attempt in range(50):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state == "Z":
            return True
        time.sleep(0.02)
    return False


def prepare_local(locator: Mapping[str, object], command: Sequence[str]) -> Execution:
    """Test-only package profile; no user switching or target overrides."""
    try:
        host_uid = pwd.getpwnam(str(locator["user"])).pw_uid
        target_uid = pwd.getpwnam(str(locator["target_user"])).pw_uid
    except KeyError:
        raise CommandError(category="argument", code="invalid_locator") from None
    return Execution(
        command,
        str(locator["chdir"]),
        {"EXEC_PROBE": "literal-$HOME"},
        validate_environment,
        host_uid,
        target_uid,
        ["/usr/bin/id", "-u"],
    )


def elapsed_value(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError("Expected an elapsed duration")
    return float(value)


def main() -> None:
    spec = exec_argument_spec()
    spec.update(
        {
            "case": {"type": "str", "required": True, "choices": [*SCRIPTS, "sleep"]},
            "secret": {"type": "str", "required": True, "no_log": True},
            "timeout_executable": {"type": "path", "default": "/usr/bin/timeout"},
        }
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    case = module.params["case"]
    data = b"\0\xff" + module.params["secret"].encode("utf-8")
    argv = (
        ["/bin/sleep", "60"]
        if case == "sleep"
        else [sys.executable, "-c", SCRIPTS[case]]
    )
    if case == "literal":
        argv.extend(["$HOME", "${HOME}", "~", "a b", ";"])
    local_profile = LocatorProfile(
        "package",
        frozenset({"user", "target_user", "chdir"}),
        prepare_local,
        {
            "deadline": "GNU timeout at the explicitly checked path; same Deadline",
            "cleanup": "Local process group only; tested ordinary and TERM-resistant descendants",
            "identity": "Host effective UID and /usr/bin/id -u before write",
            "environment": "Test container inherited environment plus checked EXEC_PROBE",
        },
    )
    started = time.monotonic()
    try:
        context = local_profile.resolve(module.params["exec"], argv)
        if case == "host_identity":
            context = replace(context, host_uid=os.geteuid() + 1)
        if case == "target_identity":
            context = replace(context, target_uid=os.geteuid() + 1)
        result = run_bounded(
            module,
            context,
            profile=TimeoutProfile(
                executable=module.params["timeout_executable"], kill_after=0.2
            ),
            deadline=Deadline(module.params["deadline"]),
            stdin=(
                data
                if case in {"roundtrip", "rejected"}
                else b"x" * 1048576
                if case in {"stdin_holder", "short_stdin"}
                else None
            ),
            max_output_bytes=1024,
            write=case
            in {"host_identity", "target_identity", "write", "sleep", "short_stdin"},
        )
        if case == "rejected" and result.rc != 0:
            # Product-owned diagnostic mapping: never return the command's text.
            raise ApiError(
                "Product rejected the operation", category="product", code="rejected"
            )
        checked = result.rc == (124 if case == "status" else 0)
        if case == "roundtrip":
            checked = checked and result.stdout == data and result.stderr == data
        elif case == "literal":
            checked = checked and json.loads(result.stdout) == argv[3:]
        elif case == "environment":
            checked = checked and result.stdout == b"literal-$HOME\n"
        if not checked:
            raise CommandError(category="response", code="invalid_command_result")
    except ApiError as error:
        failed_result = getattr(error, "result", None)
        changed = getattr(error, "write_attempted", False)
        fail_from(
            module,
            error,
            schema=ErrorSchema(
                merge_error_codes(
                    ERROR_CODES,
                    {
                        "internal": {None},
                        "argument": {"invalid_deadline"},
                        "product": {"rejected"},
                    },
                ),
                ("internal", None),
                {
                    "elapsed": elapsed_value,
                    "deadline_expired": bool,
                    "cleanup_triggered": bool,
                    "descendant_stopped": bool,
                },
            ),
            msg="Command probe failed",
            changed=changed,
            outcome="unknown" if changed else "unchanged",
            fields={
                "elapsed": time.monotonic() - started,
                "deadline_expired": bool(
                    failed_result and failed_result.deadline_expired
                ),
                "cleanup_triggered": bool(
                    failed_result and failed_result.cleanup_triggered
                ),
                "descendant_stopped": descendant_stopped(failed_result, case),
            },
        )
    module.exit_json(changed=False, checked=checked, elapsed=time.monotonic() - started)


if __name__ == "__main__":
    main()
