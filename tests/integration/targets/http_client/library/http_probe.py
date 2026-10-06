#!/usr/bin/python
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)

from __future__ import annotations

DOCUMENTATION = r"""
---
module: http_probe
short_description: Exercise the foundata.utils HTTP transport from a task
description:
  - >-
    A test-only consumer of the foundata.utils module utilities: it builds the client from the shared
    options, sends one request and reports what the transport returned.
  - "It is part of the integration tests and is not shipped with the collection."
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
        }
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    params = module.params
    headers = {"Accept": "application/json"}
    if params["token"]:
        headers["Authorization"] = f"Bearer {params['token']}"
    started = time.monotonic()
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
        fail_from(
            module,
            error,
            schema=SCHEMA,
            msg="The probe request failed",
            changed=False,
            outcome="unchanged",
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
