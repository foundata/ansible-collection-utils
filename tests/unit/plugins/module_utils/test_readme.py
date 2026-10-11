# SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute the documented consumer with real Ansible arguments and JSON exits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ansible.module_utils import basic

from ansible_collections.foundata.utils.plugins.module_utils import http_client

INPUT_SECRET = "SYNTHETIC-INPUT-SECRET-129e"
RETURNED_SECRET = "UNKNOWN-RETURNED-SECRET-b912"
README = Path(__file__).resolve().parents[4] / "README.md"


def run_example(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    responses: list[Any],
    **options: Any,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    params = {
        "url": "https://localhost",
        "api_key": INPUT_SECRET,
        "name": "web",
        "_ansible_remote_tmp": "/tmp",
        "_ansible_keep_remote_files": False,
        "_ansible_tracebacks_for": ["error"],
        **options,
    }
    monkeypatch.setattr(
        basic, "_ANSIBLE_ARGS", json.dumps({"ANSIBLE_MODULE_ARGS": params}).encode()
    )
    if hasattr(basic, "_ANSIBLE_PROFILE"):
        monkeypatch.setattr(basic, "_ANSIBLE_PROFILE", "legacy")
    monkeypatch.setattr(basic.AnsibleModule, "_log_invocation", lambda self: None)
    calls: list[tuple[str, str]] = []

    def request(
        self: http_client.HttpClient, method: str, path: str, **kwargs: Any
    ) -> http_client.HttpResponse:
        calls.append((method, path))
        document = responses.pop(0)
        if isinstance(document, Exception):
            raise document
        body = (
            document if isinstance(document, bytes) else json.dumps(document).encode()
        )
        return http_client.HttpResponse(200, {}, body)

    monkeypatch.setattr(http_client.HttpClient, "request", request)
    example = (
        README.read_text(encoding="utf-8")
        .split("```python\n", 1)[1]
        .split("\n```", 1)[0]
    )
    with pytest.raises(SystemExit):
        exec(compile(example, str(README), "exec"), {})
    result = json.loads(capsys.readouterr().out)
    new_secrets = result.pop("_ansible_new_secrets", None)
    assert new_secrets is None or new_secrets == [INPUT_SECRET]
    assert INPUT_SECRET not in json.dumps(result)
    assert RETURNED_SECRET not in json.dumps(result)
    return result, calls


@pytest.mark.parametrize("mode", ["unchanged", "changed", "check"])
def test_example_projects_public_fields_on_every_success_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    name = "web/a b?c"
    before = {
        "name": name,
        "enabled": mode == "unchanged",
        "old_token": RETURNED_SECRET,
    }
    after = {**before, "enabled": True}
    responses = [before, after, after] if mode == "changed" else [before]
    result, calls = run_example(
        monkeypatch, capsys, responses, name=name, _ansible_check_mode=mode == "check"
    )
    assert result["item"] == {"name": name, "enabled": True}
    assert result["changed"] is (mode != "unchanged")
    assert result["changed_fields"] == ([] if mode == "unchanged" else ["enabled"])
    assert result["verified"] == ("predicted" if mode == "check" else "observed")
    assert [method for method, path in calls] == (
        ["GET", "POST", "GET"] if mode == "changed" else ["GET"]
    )
    assert all(path == "/api/items/web%2Fa%20b%3Fc" for method, path in calls)


@pytest.mark.parametrize(
    "document", [[], {"name": "wrong", "enabled": True}, {"name": "web", "enabled": 1}]
)
def test_example_rejects_malformed_or_mismatched_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], document: Any
) -> None:
    result, calls = run_example(monkeypatch, capsys, [document])
    assert result["failed"] and not result["changed"]
    assert result["error_code"] == "invalid_item" and result["outcome"] == "unchanged"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "options, code",
    [
        ({"deadline": 0}, "invalid_deadline"),
        ({"url": "not-a-url"}, "invalid_url"),
        ({"name": ".."}, "invalid_name"),
    ],
)
def test_example_catches_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, Any],
    code: str,
) -> None:
    result, calls = run_example(monkeypatch, capsys, [], **options)
    assert result["failed"] and not result["changed"]
    assert result["error_code"] == code and result["outcome"] == "unchanged"
    assert not calls


@pytest.mark.parametrize("stage", ["write", "readback"])
def test_example_retains_attempt_through_parsing_and_readback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    item = {"name": "web", "enabled": False}
    responses = [item, b"not json"] if stage == "write" else [item, {}, []]
    result, calls = run_example(monkeypatch, capsys, responses)
    assert result["failed"] and result["changed"]
    assert result["outcome"] == "unknown"
    assert result["error_code"] == (
        "invalid_json" if stage == "write" else "invalid_item"
    )
    assert [method for method, path in calls].count("POST") == 1
