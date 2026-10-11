# Ansible collection: `foundata.utils`

<!--
SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
SPDX-License-Identifier: GPL-3.0-or-later
-->

This repository contains the `foundata.utils` Ansible Collection: shared
transport and safety mechanics for foundata's product collections. It ships
module utilities and documentation fragments only. There are no modules,
roles or playbooks; a product collection imports the utilities into its own
modules.

The collection depends on ansible-core alone and supports the same floors as
its consumers: ansible-core 2.19 or newer on the controller, Python 3.9 or
newer where a module runs.



<!-- rumdl-disable MD033 -->
<!-- HTML for consistent rendering across limited platform parsers -->
<div align="center" id="project-readme-header">
<br>
<br>

**⭐ Found this useful? Support open-source and star this project:**

[![GitHub repository](https://img.shields.io/github/stars/foundata/ansible-collection-utils.svg)](https://github.com/foundata/ansible-collection-utils)

<br>
</div>
<!-- rumdl-enable MD033 -->


## Table of contents<a id="toc"></a>

- [Included content](#content)
  - [`errors`](#errors)
  - [`deadline`](#deadline)
  - [`http_client`](#http-client)
  - [Documentation fragment `foundata.utils.http_client`](#fragment-http)
  - [`exec`](#exec)
  - [Documentation fragment `foundata.utils.exec`](#fragment-exec)
  - [Shared error codes](#error-codes)
- [Limits of the elapsed-time budget](#budget-limits)
- [Using the helpers in a product module](#usage)
- [Dependencies](#dependencies)
- [Development and testing](#development)
- [Licensing, copyright](#licensing-copyright)
- [Author information](#author-information)



## Included content<a id="content"></a>

Every helper lives in `plugins/module_utils/` and is imported as
`ansible_collections.foundata.utils.plugins.module_utils.<name>`. The supported
symbols are listed below; incidental imports from other modules are not
interfaces. Names with a leading underscore are internal.

|    Module     | Supported symbols |
| ------------- | ----------------- |
| `errors`      | `ApiError`, `TransportTimeout`, `ErrorSchema`, `FailsJson`, `fail_from`, `merge_error_codes`, `OUTCOMES`, `RESERVED_RESULT_KEYS` |
| `deadline`    | `Deadline`, `ERROR_CODES` |
| `http_client` | `HttpClient`, `HttpResponse`, `decode_json`, `http_argument_spec`, `ERROR_CODES`, `HTTP_METHODS`, `RESERVED_HEADERS`, `DEFAULT_TIMEOUT_SECONDS`, `DEFAULT_DEADLINE_SECONDS`, `DEFAULT_MAX_BODY_BYTES` |
| `exec`        | `Execution`, `TimeoutProfile`, `LocatorProfile`, `CommandResult`, `CommandError`, `CommandTimeout`, `RunsCommand`, `run_bounded`, `exec_argument_spec`, `ERROR_CODES`, `DEFAULT_DEADLINE_SECONDS`, `DEFAULT_MAX_OUTPUT_BYTES` |

Nothing is published yet; helpers and consumers change together. At first
publication utils and its consumers are released together; thereafter public
signatures, exceptions, defaults and fragment options are versioned interfaces.


### `errors`<a id="errors"></a>

The exception base, the error schema and the safe failure call.

What it guarantees:

- `ApiError` is the one catch boundary. A product derives its own failure
  type from it and catches `ApiError` at the module boundary; that catch
  also covers `TransportTimeout`, which a product subclass alone would miss.
  Each error carries a local `category`, a local `code` or `None`, and an
  HTTP `status` or `None`.
- `fail_from(module, error, schema=..., msg=..., changed=..., outcome=...,
  fields=...)` calls `fail_json` with a local message, the error's category
  and code validated against the product's `ErrorSchema` (unknown pairs
  become the schema's fixed fallback), the HTTP status when it is one, the
  caller's `changed` and `outcome`, and every extra field after its
  validator ran. It passes `exception=None` and never publishes the error's
  text, so a product response that leaked into an exception message does not
  reach the result, the traceback or the log, even with
  `ANSIBLE_DISPLAY_TRACEBACK` enabled.
- `fail_from` refuses result keys that Ansible interprets or that it sets
  itself, refuses an `outcome` that disagrees with `changed`, and refuses a
  field without a validator. Those are programming errors and raise
  `ValueError` or `TypeError` without chaining the handled error.
- `merge_error_codes()` folds the `ERROR_CODES` mappings of the helpers into
  a product schema.

What it does not do:

- It does not bound or redact text. Nothing in the collection publishes
  product text; a product parser turns product output into local codes.
- It does not decide `changed` or `outcome`. The caller tracks mutation.
- It does not check a field's name against the success result. Choose
  failure field names that do not appear in the module's success result
  with another type; a mapping on success and a list on failure under one
  key confuses every caller that reads both paths.


### `deadline`<a id="deadline"></a>

One monotonic elapsed-time budget per module operation.

What it guarantees:

- `Deadline(seconds)` starts a budget when created. `remaining()` returns
  the seconds left and raises `TransportTimeout` with code
  `deadline_exceeded` once the budget is exhausted, on every later call too.
- A non-positive, non-finite or non-numeric budget raises `ApiError` with
  category `argument` and code `invalid_deadline`, so a bad module option
  fails like any other expected failure.
- A transport never resets a deadline. The caller shares one deadline across
  retries, polling, read-back and command execution.

What it does not do:

- It does not interrupt anything. A blocking step that ignores the budget
  runs to its own end; see [the limits](#budget-limits).


### `http_client`<a id="http-client"></a>

A bounded byte transport over `ansible.module_utils.urls.open_url`.

`HttpClient(base_url, user_agent=..., validate_certs=True, ca_path=None,
use_proxy=True, timeout=30, max_body_bytes=4 MiB)` is configured once per
endpoint. `request(method, path, deadline=..., headers=..., body=None)`
returns an `HttpResponse` with `status`, lower-cased `headers` and the raw
`body` bytes.

What it guarantees:

- Bytes in, bytes out. The product encodes JSON or forms, sets its
  authentication and product headers, decodes the body and checks the HTTP
  and the application status. The transport does no retry and no
  pagination.
- The request stays within the endpoint: the path must be absolute, without
  dot segments, a fragment, a backslash, whitespace or control characters,
  and the resulting URL must keep the endpoint's origin and path prefix. A
  base URL with credentials, a query or a fragment is refused.
- Certificates are validated by default; `ca_path` adds a private authority
  and must name an existing regular file; failures loading it are translated
  locally. `validate_certs=False` is never chosen automatically.
- Redirects are not followed: a 3xx response is returned like any other, so
  credentials never travel to another origin. `netrc` is not read. Only the
  `identity` content encoding is requested and accepted; a compressed
  response is refused. Products cannot set `Accept-Encoding`, `User-Agent`,
  `Host`, `Content-Length`, `Transfer-Encoding` or `Connection`.
- The socket inactivity timeout is the smaller of `timeout` and the
  remaining deadline. The body is read with `read1`, with a budget check
  before and after every read and a byte limit. A declared `Content-Length`
  beyond the limit is refused before the first read; a body shorter than its
  declared length is a protocol failure, not a truncated success.
  HEAD and 304 responses have no body; their representation length is not a
  received-body size and is exempt from these checks.
- Every response is closed, including an error status, a refused encoding,
  an oversized body and a failed read.
- An HTTP error status is a response, not an exception: its body is read
  under the same bounds and returned, so a product parser can derive an
  allowlisted diagnostic.
- Failures become `ApiError` with the categories and codes in
  `http_client.ERROR_CODES`: `argument` (`invalid_url`, `invalid_timeout`,
  `invalid_ca_path`), `request` (`invalid_path`), `response`
  (`body_too_large`, `content_encoding`, `invalid_utf8`, `invalid_json`),
  `transport` (`connection_failed`,
  `tls_validation`, `io_error`, `http_protocol`) and `timeout`
  (`socket_timeout`, `deadline_exceeded`, both as `TransportTimeout`).
  Translated exception messages are local and suppress the original displayed
  exception chain, so no URL, header or server text enters those messages.
- `decode_json(body)` turns a UTF-8 JSON body into Python values and raises
  `response` errors `invalid_utf8` and `invalid_json` without the body text.
  An empty body is not a document: a product that answers a write without a
  body checks `body` before it decodes. The shape of the decoded value is
  the product's to validate.
- Header values never enter a `repr`: `HttpResponse` prints its status only
  and `HttpClient` its base URL only. The client stores no headers.
- `http_argument_spec()` returns a fresh specification of the mechanical
  options on every call; extending the copy affects no other module.

What it does not do:

- It does not check the application status, decode anything, authenticate
  or paginate. Those are product semantics.
- It does not keep connections open. Each request opens its own.
- It does not make the budget a hard limit; see the next section.


### Documentation fragment `foundata.utils.http_client`<a id="fragment-http"></a>

Documents `url`, `validate_certs`, `ca_path`, `use_proxy`, `timeout` and
`deadline`, matching `http_argument_spec()`. Credential options stay with
the product, which may also override an option's description, for example
to name an environment variable fallback. Each consumer validates its
merged documentation against its real argument specification.



### `exec`<a id="exec"></a>

`run_bounded(module, execution, profile=..., deadline=..., stdin=None,
max_output_bytes=1 MiB, write=False)` executes one prepared command on a POSIX
execution host. Import
the utility as `from ...module_utils import exec as exec_util`.

What it guarantees:

- `Execution` carries argv, absolute working directory, host environment
  additions, an effective-environment validator, host and application UIDs,
  and a separate application identity command. Products validate executable
  paths, identifiers, prefixes and option boundaries before preparing it.
- `module.run_command` receives an argument list, `encoding=None`,
  `binary_data=True`, `check_rc=False`, `handle_exceptions=False`,
  `use_unsafe_shell=False` and `expand_user_and_vars=False`. Literal `$HOME`,
  `${HOME}` and `~` stay literal. Stdin is bytes or `None`; empty bytes are
  refused because core does not open a pipe for them.
- A synchronous selector supervisor writes stdin without blocking, reads
  each stream under its own byte limit and checks the shared `Deadline`.
  Both streams are checked before decoding, parsing or returning them.
  `CommandResult` contains `rc`, byte `stdout` and `stderr`,
  `deadline_expired` and `cleanup_triggered`. Streams and execution data
  are omitted from repr; raw streams never become diagnostics or Ansible
  results from this helper. Command argv must be non-secret and safe for
  framework debug logging; stdin and captured streams are not logged by this
  helper. Execution repr suppression is not a framework logging guarantee.
- GNU coreutils `timeout` at the `TimeoutProfile.executable` absolute path
  (default `/usr/bin/timeout`) is required. Its fallback duration is
  `ceil(remaining) + 1`, never zero; the selector observes expiry directly.
  Command exits 124, 137 and negative signal statuses alone are not expiry.
  `kill_after` defaults to one second and is separate from the deadline.
  TERM grace and each of at most two reap waits use that allowance; process
  creation, scheduling and uninterruptible kernel work are not hard-bounded.
- The supervisor sends TERM, then KILL when needed, to the wrapper's process
  group. It also cleans ordinary descendants after a parent exits. A
  cleanup intervention is a failure, even if the parent returned zero.
  Cleanup waiting is bounded; process creation and uninterruptible kernel
  work are not hard real-time operations.
- The environment validator sees the actual inherited environment, the
  module's `run_command_environ_update`, and the execution's additions,
  including core's `PYTHONPATH` cleanup. An invalid working directory or
  environment fails before starting a command. No environment is silently
  removed; the product must document and enforce its inherited policy.
- With `write=True`, the runner compares the host effective UID and then
  executes `identity_argv` under the same deadline. Its bounded output must
  equal the expected numeric application UID. The product must build this
  probe through the same container or wrapper as the real command.
- Mechanical failures are `ApiError`: `CommandError` for execution, size,
  environment, identity or cleanup failures; `CommandTimeout`, also a
  `TransportTimeout`, for observed expiry. Errors from a supervised command
  carry its bounded internal `result` when available. Preflight failures
  carry no result. Original exception text is discarded. The helper never
  retries.
- Command errors expose `write_attempted`: true only when the requested
  `write=True` invocation started, not for its identity probe or a preflight
  failure. This is attempt metadata, not proof of mutation. A deadline
  exhausted before execution raises the base `TransportTimeout` without it.

What it does not do:

- It does not decode output or decide whether a product status is success.
  Nonzero exits return a `CommandResult`; a reviewed product parser checks
  the status and shape, and maps diagnostics to local codes. Raw output
  must never reach `exit_json`, `fail_json`, warnings, diffs or logs.
- It does not decide mutation outcome. Count completed write calls in the
  product through parsing and read-back. An interrupted or uncertain write uses
  `fail_from(..., changed=True, outcome="unknown")`; reconcile by identity
  before any retry, using the original deadline. On failure, preserve earlier
  attempts and include `getattr(error, "write_attempted", False)` for this call.
- It does not switch the module user. Use Ansible `become`. It does not
  kill detached processes, container-side processes or remote work merely
  because their host client stopped. Such deployments need their own
  cleanup qualification and must retain an unknown write outcome.

`LocatorProfile` declares a product's accepted fields, a product-owned
`prepare(locator, command)` function and four required qualification records:
`deadline`, `cleanup`, `identity`, `environment`. `resolve()` rejects a wrong
kind or unused non-null fields before product validation. Core inserts null
values for omitted suboptions; these are not treated as supplied fields.
The prepare function rejects conflicting values and builds both command and
identity prefixes. The declared forms are:

|   Kind    |                                          Command and identity channel                                           | Qualification boundary |
| --------- | --------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `package` | Absolute local binary; host UID plus an independent effective-UID probe                                         | Local GNU timeout/process-group mechanics exercised by the integration target; product binary remains product-owned |
| `podman`  | Product-validated engine prefix with explicit container and application-user options for both command and probe | Product must qualify engine version, container identity, target environment and in-container survivors after expiry |
| `custom`  | Validated fixed wrapper prefix, including a documented application-user transition                              | Product must qualify the wrapper's identity probe, target environment, deadline propagation and cleanup; this is trusted execution, not confinement |

Only the local process mechanics are qualified here. These declarations do
not advertise tested Zammad, Nextcloud, Podman, Docker or user-switching
wrappers. Their consumers supply and test the product records before use.


### Documentation fragment `foundata.utils.exec`<a id="fragment-exec"></a>

Documents `deadline` and the `exec` locator dictionary, matching the fresh
`exec_argument_spec()`. Products narrow choices and suboptions and document
their supported profiles. `env` and `target_env` are `no_log` maps, but
secrets must still use stdin because prefixes can expose environment values
as process arguments. Consumers validate merged documentation and specs.


### Shared error codes<a id="error-codes"></a>

Merge the used helpers' `ERROR_CODES` with the product schema through
`errors.merge_error_codes`. `errors` adds no transport codes. The full union:

|  Category   | Codes |
| ----------- | ----- |
| `argument`  | `invalid_ca_path`, `invalid_deadline`, `invalid_environment`, `invalid_execution`, `invalid_locator`, `invalid_timeout`, `invalid_url` |
| `identity`  | `host_identity`, `target_identity` |
| `request`   | `invalid_path` |
| `response`  | `body_too_large`, `content_encoding`, `invalid_command_result`, `invalid_json`, `invalid_utf8`, `output_too_large` |
| `timeout`   | `deadline_exceeded`, `socket_timeout` |
| `transport` | `command_io`, `command_start`, `connection_failed`, `descendant_cleanup`, `http_protocol`, `io_error`, `tls_validation` |


## Limits of the elapsed-time budget<a id="budget-limits"></a>

The HTTP deadline is cooperative. The transport checks it before it opens a
connection and before and after each body read, and derives the socket
timeout from it. Everything between two checks can run past the budget:

|                      Situation                      |                                                What bounds it                                                | Measured in the unit tests |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------- |
| The server accepts and sends nothing                | The socket timeout, `min(timeout, remaining)`                                                                | 0.52 s with `timeout=0.5`; 0.52 s with `timeout=30` and a 0.5 s deadline |
| The response header arrives one byte at a time      | Nothing. Every byte resets the inactivity timer, and the budget is first checked once the header is complete | 1.94 s for a 98-byte header at 20 ms per byte against a 0.5 s deadline |
| The body arrives one byte at a time                 | The budget, checked after each `read1`                                                                       | 0.53 s against a 0.5 s deadline |
| The body stalls after a few bytes                   | The socket timeout as set when the connection was opened                                                     | 0.32 s with a 0.3 s deadline and `timeout=0.6` |
| DNS resolution                                      | Nothing in this transport. The resolver's own timeouts apply                                                 | Not measured               |
| A slow proxy, chunk framing of a single large chunk | The socket timeout per underlying read                                                                       | Not measured               |

The times come from `tests/unit/plugins/module_utils/test_http_client.py` on a
loopback server; they are printed with `pytest -s`. A module that needs a
hard limit needs the task-level `timeout`, which stops the controller
waiting but does not stop the module process; an interrupted write then has
an unknown outcome.



## Using the helpers in a product module<a id="usage"></a>

A product collection wraps the transport once: it sets authentication and
content headers, translates the HTTP status into its own codes, decodes the
body. The module tracks write attempts and calls that wrapper, never
`HttpClient` directly. In this example only `name` and `enabled` are documented
public item fields; all other response fields stay internal.

```python
import json
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.foundata.utils.plugins.module_utils import (
    deadline as deadline_util,
)
from ansible_collections.foundata.utils.plugins.module_utils import (
    http_client as http_util,
)
from ansible_collections.foundata.utils.plugins.module_utils.deadline import Deadline
from ansible_collections.foundata.utils.plugins.module_utils.errors import (
    ApiError,
    ErrorSchema,
    fail_from,
    merge_error_codes,
)
from ansible_collections.foundata.utils.plugins.module_utils.http_client import (
    HttpClient,
    decode_json,
)

SCHEMA = ErrorSchema(
    codes_by_category=merge_error_codes(
        http_util.ERROR_CODES,
        deadline_util.ERROR_CODES,
        {
            "argument": {"invalid_name"},
            "auth": {"unauthenticated", "forbidden"},
            "internal": {None},
            "product": {"rejected", "unexpected_status", "not_found"},
            "response": {"invalid_item"},
            "verification": {"not_persisted"},
        },
    ),
    fallback=("internal", None),
)


class ProductError(ApiError):
    """A failure the product client classified; never carries product text."""


def item_path(name):
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or name in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ProductError(
            "Invalid item name", category="argument", code="invalid_name"
        )
    return "/api/items/" + quote(name, safe="")


def parse_item(document, name):
    if (
        not isinstance(document, dict)
        or document.get("name") != name
        or not isinstance(document.get("enabled"), bool)
    ):
        raise ProductError(
            "Invalid item response", category="response", code="invalid_item"
        )
    return {"name": document["name"], "enabled": document["enabled"]}


class Client:
    """One authenticated endpoint for one module invocation."""

    def __init__(self, params):
        self._http = HttpClient(
            params["url"],
            user_agent="foundata.example/1.0",
            validate_certs=params["validate_certs"],
            ca_path=params["ca_path"],
            use_proxy=params["use_proxy"],
            timeout=params["timeout"],
        )
        self.deadline = Deadline(params["deadline"])
        self._headers = {
            "Authorization": f"Bearer {params['api_key']}",
            "Accept": "application/json",
        }

    def call(self, method, path, body=None):
        headers = dict(self._headers)
        data = None
        if body is not None:  # Content-Type only when a body travels
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._http.request(
            method, path, deadline=self.deadline, headers=headers, body=data
        )
        if response.status in (401, 403) or 300 <= response.status < 400:
            code = "forbidden" if response.status == 403 else "unauthenticated"
            raise ProductError(
                "Authentication failed",
                category="auth",
                code=code,
                status=response.status,
            )
        if response.status == 404:
            raise ProductError(
                "Not found", category="product", code="not_found", status=404
            )
        if (
            response.status >= 400
        ):  # the product refused the request; its text stays unread
            raise ProductError(
                "Request rejected",
                category="product",
                code="rejected",
                status=response.status,
            )
        if response.status != 200:
            raise ProductError(
                "Unexpected status",
                category="product",
                code="unexpected_status",
                status=response.status,
            )
        return decode_json(response.body)


spec = http_util.http_argument_spec()
spec["api_key"] = {"type": "str", "required": True, "no_log": True}
spec["name"] = {"type": "str", "required": True}
module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
attempts = 0
try:
    client = Client(module.params)
    name = module.params["name"]
    path = item_path(name)
    current = parse_item(client.call("GET", path), name)
    if current["enabled"]:
        module.exit_json(
            changed=False, item=current, changed_fields=[], verified="observed"
        )
    if module.check_mode:
        module.exit_json(
            changed=True,
            item={**current, "enabled": True},
            changed_fields=["enabled"],
            verified="predicted",
        )
    attempts += (
        1  # retained through response parsing and read-back; never retry blindly
    )
    client.call("POST", path, {"enabled": True})
    after = parse_item(
        client.call("GET", path), name
    )  # verify the public postcondition
    if not after["enabled"]:
        raise ProductError(
            "Change did not persist", category="verification", code="not_persisted"
        )
    module.exit_json(
        changed=True, item=after, changed_fields=["enabled"], verified="observed"
    )
except ApiError as error:
    fail_from(
        module,
        error,
        schema=SCHEMA,
        msg="Enabling the item failed",
        changed=attempts > 0,
        outcome="unknown" if attempts > 0 else "unchanged",
    )
```

The helper supplies these allowlisted failure fields. Ansible may add
invocation metadata and a message-free call stack; devel may carry declared
`no_log` values in its private controller protocol. Neither the handled
exception text nor raw product responses are published by `fail_from`.

```json
{
  "failed": true,
  "msg": "Enabling the item failed",
  "error_category": "product",
  "error_code": "rejected",
  "http_status": 409,
  "changed": true,
  "outcome": "unknown"
}
```

The documentation of such a module extends the fragment:

```yaml
extends_documentation_fragment:
  - foundata.utils.http_client
```



## Dependencies<a id="dependencies"></a>

None beyond ansible-core. See `dependencies` in [`galaxy.yml`](./galaxy.yml).



## Development and testing<a id="development"></a>

[`DEVELOPMENT.md`](./DEVELOPMENT.md) describes the checks. `antsibull-nox.toml`
lists the sessions: `CI=true nox` for check-only lint, documentation, license,
build and import, `nox -s ansible-test-sanity`, `nox -s ansible-test-units` and
`nox -s ansible-test-integration` for the containerized `ansible-test` runs on
every supported ansible-core version.



## Licensing, copyright<a id="licensing-copyright"></a>

<!--REUSE-IgnoreStart-->
<!-- should match SPDX-PackageSupplier -->
Copyright (c) 2026, foundata GmbH <https://foundata.com>

This project is licensed under the GNU General Public License v3.0 or later
(SPDX-License-Identifier: `GPL-3.0-or-later`), see
[`LICENSES/GPL-3.0-or-later.txt`](./LICENSES/GPL-3.0-or-later.txt) for the full
text.

The [`REUSE.toml`](./REUSE.toml) file provides detailed licensing and copyright
information in a human- and machine-readable format. This includes parts that
may be subject to different licensing or usage terms, such as third-party
components. The repository conforms to the
[REUSE specification](https://reuse.software/spec/). You can use
[`reuse spdx`](https://reuse.readthedocs.io/en/latest/readme.html#cli) to create
an
[SPDX software bill of materials (SBOM)](https://en.wikipedia.org/wiki/Software_Package_Data_Exchange).
<!--REUSE-IgnoreEnd-->



### Trademarks<a id="trademarks"></a>

Third-party trademarks used in this repository:

- Ansible® is a trademark of Red Hat, LLC, registered in the United States and
  other countries.

Their use here is purely descriptive and does not imply any affiliation with or
endorsement by the trademark holders.

Own and licensed trademarks used in this repository:

- foundata® is a trademark of [IPAM GmbH](https://ipam-services.com/),
  registered in Germany and the European Union, licensed to
  [foundata GmbH](https://foundata.com/).


## Author information<a id="author-information"></a>

This [project](https://foundata.com/en/projects/) was created and is maintained
by [foundata](https://foundata.com/).

Initially based on an
[Ansible skeleton](https://foundata.com/en/projects/ansible-skeletons/)
developed by [foundata](https://foundata.com/).
