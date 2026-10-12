#!/usr/bin/python
# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

DOCUMENTATION = r"""
---
module: http_probe
short_description: Exercise the foundata.utils HTTP transport from a task
description:
  - >-
    A test-only consumer of the foundata.utils module utilities: it builds the client from the shared
    options, sends one request and reports what the transport returned.
  - "It ships as test material, not as a supported collection module."
version_added: "1.0.0"
author:
  - foundata Development Team (@foundata)
options:
  method:
    description: "HTTP method of the request."
    type: str
    default: "GET"
    choices: ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
  path:
    description: "Request path within O(url), with an optional query string."
    type: str
    default: "/"
  token:
    description: "A synthetic bearer token sent in the C(Authorization) header."
    type: str
  return_body:
    description: "Whether to return the decoded response body, as a product module would return a parsed field."
    type: bool
    default: false
  max_body_bytes:
    description: "Byte limit of the response body."
    type: int
    default: 4194304
  reconcile_path:
    description:
      - >-
        Path of a state read that settles a write whose request raised, as a product reconciles by identity.
        The read returns C({"present": true}) once the write took effect.
      - "Only used for methods other than C(GET) and C(HEAD). Without it the probe reports every failure as unchanged."
    type: str
extends_documentation_fragment:
  - foundata.utils.http_client
"""

EXAMPLES = r"""
---
- name: "Probe a loopback endpoint"
  http_probe:
    url: "http://127.0.0.1:8080"
    path: "/ok"
    deadline: 5
  register: __probe
"""

RETURN = r"""
---
status:
  description: HTTP status of the response.
  returned: success
  type: int
  sample: 200
headers:
  description: Response headers with lower-cased names.
  returned: success
  type: dict
body:
  description: The decoded response body, bounded to 4096 characters.
  returned: success and O(return_body=true)
  type: str
elapsed:
  description: Seconds the request took.
  returned: always
  type: float
reconciled:
  description: Whether the state read after a failed write found the write's effect.
  returned: success after a failed write with O(reconcile_path)
  type: bool
write_error_code:
  description: The shared error code of the write whose effect the state read established.
  returned: success after a failed write with O(reconcile_path)
  type: str
"""

import time

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


def _path(value: object) -> str:
    """Publish the request path the caller supplied."""
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    return value


def _elapsed(value: object) -> float:
    """Publish a measured duration in seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("elapsed must be a number of seconds")
    return float(value)


SCHEMA = ErrorSchema(
    codes_by_category=merge_error_codes(
        http_util.ERROR_CODES, deadline_util.ERROR_CODES, {"internal": {None}}
    ),
    fallback=("internal", None),
    field_validators={"path": _path, "elapsed": _elapsed},
)


def _reconcile(
    client: HttpClient, deadline: Deadline, path: str, headers: dict[str, str]
) -> bool | None:
    """Return whether the write took effect, or ``None`` when that is unknown."""
    try:
        response = client.request("GET", path, deadline=deadline, headers=headers)
        if response.status != 200:
            return None
        document = decode_json(response.body)
    except ApiError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("present"), bool):
        return None
    return document["present"]


def main() -> None:
    spec = http_util.http_argument_spec()
    spec.update(
        {
            "method": {
                "type": "str",
                "default": "GET",
                "choices": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
            },
            "path": {"type": "str", "default": "/"},
            "token": {"type": "str", "no_log": True},
            "return_body": {"type": "bool", "default": False},
            "max_body_bytes": {"type": "int", "default": 4 * 1024 * 1024},
            "reconcile_path": {"type": "str"},
        }
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    params = module.params
    headers = {"Accept": "application/json"}
    if params["token"]:
        headers["Authorization"] = f"Bearer {params['token']}"
    started = time.monotonic()
    # A write counts as attempted once its request is sent; only a state read
    # by identity can tell what it did.
    attempted = bool(params["reconcile_path"]) and params["method"] not in (
        "GET",
        "HEAD",
    )
    client: HttpClient | None = None
    deadline: Deadline | None = None
    try:
        client = HttpClient(
            params["url"],
            user_agent="foundata.utils-http-probe/1.0",
            validate_certs=params["validate_certs"],
            ca_path=params["ca_path"],
            use_proxy=params["use_proxy"],
            timeout=params["timeout"],
            max_body_bytes=params["max_body_bytes"],
        )
        deadline = Deadline(params["deadline"])
        response = client.request(
            params["method"], params["path"], deadline=deadline, headers=headers
        )
    except ApiError as error:
        present = None
        if attempted and client is not None and deadline is not None:
            present = _reconcile(client, deadline, params["reconcile_path"], headers)
        if present:
            module.exit_json(
                changed=True,
                reconciled=True,
                write_error_code=error.code,
                elapsed=time.monotonic() - started,
            )
        fail_from(
            module,
            error,
            schema=SCHEMA,
            msg="The probe request failed",
            changed=attempted and present is None,
            outcome="unknown" if attempted and present is None else "unchanged",
            fields={"path": params["path"], "elapsed": time.monotonic() - started},
        )
    result = {
        "changed": False,
        "status": response.status,
        "headers": dict(response.headers),
        "elapsed": time.monotonic() - started,
    }
    if params["return_body"]:
        result["body"] = response.body.decode("utf-8", errors="replace")[:4096]
    module.exit_json(**result)


if __name__ == "__main__":
    main()
