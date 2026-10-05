# Development

Follow the
[foundata Ansible guidelines](https://foundata.com/en/guidelines/ansible-modules/).

## Prerequisites<a id="prerequisites"></a>

- [uv](https://docs.astral.sh/uv/), or [nox](https://nox.thea.codes/) with
  [antsibull-nox](https://docs.ansible.com/projects/antsibull-nox/) in one
  Python environment.
- [Podman](https://podman.io/) for the container-backed `ansible-test` sessions.

Supply unpublished dependencies in an isolated `ANSIBLE_COLLECTIONS_PATH`.
This utility collection has no product endpoint or Molecule scenario.

## Checks<a id="checks"></a>

Run every check from the collection root, inside its Git repository:

```bash
uv run noxfile.py                 # rewrite, then check
CI=true uv run noxfile.py         # check only; fails on anything a rewrite would change
uv run noxfile.py -l              # list all sessions
uv run noxfile.py -s lint         # Python, plugin YAML and typed unit tests
uv run noxfile.py -s ansible-test-sanity
uv run noxfile.py -s ansible-test-units
uv run noxfile.py -s ansible-test-integration
```

With nox installed, `nox` replaces `uv run noxfile.py`, for example
`nox -s lint`. A plain run applies the project's own rewrites before checking:
`ansible-lint --fix`, Python and Markdown formatting.
With `CI=true` every session only checks. Arguments after `--` go to the
session's tool unchanged. `noxfile.py` documents each session; the built-in
sessions are configured in `antsibull-nox.toml`.

Select the `ansible-test-*` meta sessions by name. Tags select nothing:
antsibull-nox registers them for its CI matrix generator only.
Set `max_version` in the ansible-test session configuration to omit an
unreleased series; otherwise its lane installs the GitHub stable branch
before a PyPI release. Run one nox chain at a time per container store.

Use Python 3.13 for nox in deep checkouts: `uv run --python 3.13 noxfile.py`.
Under Python 3.14, `docs-check` can fail with `AF_UNIX path too long` because
antsibull-docs starts worker processes whose socket path includes the checkout.

The default sessions verify the foundata lint rules and validate both changelog
fragments and the generated `changelogs/changelog.yaml` separately.

## Consumers of the unpublished collection

A product collection that depends on `foundata.utils` before publication
serves it from a tree on `ANSIBLE_COLLECTIONS_PATH`: build this collection
with `ansible-galaxy collection build`, install the archive into a run-owned
path with `ansible-galaxy collection install -p <path>`, and export
`ANSIBLE_COLLECTIONS_PATH=<path>` for the consumer's checks. antsibull-nox
resolves collections through `ansible-galaxy collection list`, so every
session, the `ansible-test` lanes included, sees the copy. Do not put local
paths into `antsibull-nox.toml`. The Galaxy importer in a consumer's
`build-import-check` prints a non-fatal error for the documentation fragment
`foundata.utils.http_client` until this collection is published.

## Unit test iteration

Unit tests run directly for quick iteration with a scratch `ansible_collections`
tree that links this checkout as `foundata/utils`:

```sh
PYTHONPATH=/path/to/scratch/collections python -m pytest tests/unit -s --import-mode=importlib
```

The `-s` flag prints the measured times of the loopback transport cases.
