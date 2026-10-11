# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare parsed mechanical documentation with the actual fresh specs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from ansible.module_utils.common.yaml import yaml_load

from ansible_collections.foundata.utils.plugins.doc_fragments.exec import (
    ModuleDocFragment as ExecFragment,
)
from ansible_collections.foundata.utils.plugins.doc_fragments.http_client import (
    ModuleDocFragment as HttpFragment,
)
from ansible_collections.foundata.utils.plugins.module_utils.exec import (
    exec_argument_spec,
)
from ansible_collections.foundata.utils.plugins.module_utils.http_client import (
    http_argument_spec,
)


def compare_options(spec: dict[str, Any], docs: dict[str, Any]) -> None:
    assert spec.keys() == docs.keys()
    for name, option in spec.items():
        documented = docs[name]
        for key in ("type", "elements", "default", "choices"):
            assert option.get(key) == documented.get(key), (name, key)
        assert option.get("required", False) == documented.get("required", False)
        compare_options(option.get("options", {}), documented.get("suboptions", {}))


@pytest.mark.parametrize(
    "factory, fragment",
    [
        (http_argument_spec, HttpFragment.DOCUMENTATION),
        (exec_argument_spec, ExecFragment.DOCUMENTATION),
    ],
)
def test_fragment_matches_argument_spec(
    factory: Callable[[], dict[str, Any]], fragment: str
) -> None:
    compare_options(factory(), yaml_load(fragment)["options"])
