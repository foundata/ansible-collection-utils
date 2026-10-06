# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: foundata GmbH (https://foundata.com)
"""Mechanical locator options; consumers document their qualified profiles."""

from __future__ import annotations


class ModuleDocFragment:
    """Options returned by exec_argument_spec()."""

    DOCUMENTATION = r"""
---
requirements:
  - "POSIX execution host with Python 3.9 or newer and GNU coreutils timeout at the profile's declared absolute path."
options:
  deadline:
    description:
      - "Positive elapsed-time budget in seconds shared by identity checks, commands and read-back."
      - "Process cleanup has a separate, profile-defined grace period. Interrupted writes have an unknown outcome."
    type: int
    default: 120
  exec:
    description:
      - "Execution locator on the host running the module."
      - "The product documents supported profiles, executable prerequisites, identity checks and environment policy."
      - "Unsupported or conflicting fields are rejected by the product profile."
    type: dict
    required: true
    suboptions:
      kind:
        description: "Declared profile for a local installation, container engine or qualified wrapper."
        type: str
        required: true
        choices: ["package", "podman", "custom"]
      user:
        description: "Expected module-process user on the execution host. Establish it with Ansible become."
        type: str
        required: true
      container:
        description: "Container identity accepted by the container profile."
        type: str
      target_user:
        description: "Expected application identity, selected through container options or a documented wrapper channel."
        type: str
        required: true
      executable:
        description: "Absolute executable path accepted by the custom profile."
        type: path
      argv:
        description:
          - "Arguments before the product command, accepted only by a profile that validates this prefix."
          - "Must not contain secrets. Custom execution is trusted, not confined."
        type: list
        elements: str
      chdir:
        description: "Absolute working directory on the execution host."
        type: path
        default: "/"
      env:
        description:
          - "Host environment additions allowed by the profile; inherited variables are validated too."
          - "This map is concealed in invocation output. Send secrets on stdin, not through this option."
        type: dict
      target_chdir:
        description: "Application working directory, where the profile supports it."
        type: path
      target_env:
        description:
          - "Application environment additions allowed by the profile."
          - "The profile also qualifies the environment inherited inside the container or wrapper."
        type: dict
"""
