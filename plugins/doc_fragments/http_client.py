# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Mechanical connection options of modules built on ``foundata.utils.http_client``."""

from __future__ import annotations


class ModuleDocFragment:
    """The options ``http_argument_spec()`` declares, without credentials."""

    DOCUMENTATION = r"""
---
options:
  url:
    description:
      - "Base URL of the product's API endpoint, including the scheme and any path prefix, for example V(https://firewall.example.com/api)."
      - "Must not contain credentials, a query string or a fragment. The host must be a name IDNA can encode or an IP address."
    type: str
    required: true
  validate_certs:
    description:
      - "Whether to verify the endpoint's TLS certificate."
      - "Set to V(false) only for a test endpoint you control. Prefer O(ca_path) for a private certificate authority."
    type: bool
    default: true
  ca_path:
    description:
      - "Path to a PEM file on the execution host with the certificate authorities that sign the endpoint's certificate."
      - "The system trust store is used when omitted."
    type: path
  use_proxy:
    description:
      - "Whether to honor the proxy environment variables of the execution host, such as E(HTTPS_PROXY) and E(NO_PROXY)."
    type: bool
    default: true
  timeout:
    description:
      - "Socket inactivity timeout in seconds, applied to connecting and to each read. It is never longer than the remaining O(deadline)."
      - "Must be greater than zero and at most V(1000000000)."
    type: int
    default: 30
  deadline:
    description:
      - "Elapsed-time budget in seconds for the whole operation, shared by every request, retry, poll and read-back of a module run."
      - >-
        The budget is cooperative, not a hard kill. DNS resolution, a response header that arrives slowly and a
        slow proxy are outside the socket timeout and can exceed it. A stalled body read can exceed it by up to
        one O(timeout).
      - "Must be greater than zero and at most V(1000000000)."
    type: int
    default: 120
"""
