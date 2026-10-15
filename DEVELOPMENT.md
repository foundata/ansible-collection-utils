# Development

<!--
SPDX-FileCopyrightText: foundata GmbH <https://foundata.com>
SPDX-License-Identifier: GPL-3.0-or-later
-->

Changes follow foundata's
[Ansible module guide](https://foundata.com/en/guidelines/ansible-modules/), in
particular its rules for shared code and documentation fragments. Each module
utility's docstring states the rules it enforces and why; a change keeps them
or revises them there.

## Prerequisites<a id="prerequisites"></a>

- [uv](https://docs.astral.sh/uv/), or [nox](https://nox.thea.codes/) with
  [antsibull-nox](https://docs.ansible.com/projects/antsibull-nox/) in one
  Python environment.
- [Podman](https://podman.io/) or Docker for the container-backed `ansible-test`
  sessions. With both installed, nox uses Podman; set
  `ANTSIBULL_NOX_CONTAINER_ENGINE`, for example to `auto-prefer-docker`, to
  prefer Docker.

This utility collection has no product endpoint or Molecule scenario.

## Checks<a id="checks"></a>

`runtime-check` runs by default. It installs a built archive in isolation and
retains only `meta/`, `plugins/`, `roles/` and `playbooks/`. It checks Python
imports, module documentation and role loading/syntax, then runs available
unit tests from a separate fixture directory against that installation.
Role deployment behavior remains covered by Molecule.

To check the exact archive retained for a release:

```sh
uv run noxfile.py -s runtime-check -- --artifact /path/to/collection.tar.gz
```

Run every check from the collection root, inside its Git repository:

```bash
uv run noxfile.py                 # rewrite, then check
CI=true uv run noxfile.py         # check only; fails on anything a rewrite would change
uv run noxfile.py -l              # list all sessions
uv run noxfile.py -s lint         # Python, plugin YAML and typed unit tests
uv run noxfile.py -s helper-lint   # Python helpers
uv run noxfile.py -s ansible-test-sanity
uv run noxfile.py -s ansible-test-units
uv run noxfile.py -s ansible-test-integration
```

`helper-lint` covers Python in `extensions/molecule/`, `roles/*/files/`,
`tools/`, `scripts/` and `tests/runtime_check.py`, using [ruff.toml](ruff.toml).
It runs by default; add it to CI configurations that list sessions explicitly.

With nox installed, `nox` replaces `uv run noxfile.py`, for example
`nox -s lint`. A plain run applies the project's own rewrites before checking:
`ansible-lint --fix`, Python and Markdown formatting. With `CI=true` every
session only checks. The default sessions verify the foundata lint rules and
validate both changelog fragments and the generated `changelogs/changelog.yaml`
separately. Except for `helper-lint`, arguments after `--` go to the session's
tool unchanged.
`noxfile.py` documents each session; the built-in sessions are configured in
`antsibull-nox.toml`.

Select the `ansible-test-*` meta sessions by name. Tags select nothing:
antsibull-nox registers them for its CI matrix generator only. `-l` also lists
the single lanes; run one by its name, for example
`uv run noxfile.py -s ansible-test-units-2.21-3.14`. Run one nox chain at a time
per container store.

Set `max_version` in the ansible-test session configuration to omit an
unreleased series; otherwise its lane installs the GitHub stable branch before a
PyPI release.

The unit and integration lanes cover every admitted ansible-core version, each
on its payload floor and upper Python as `antsibull-nox.toml` sets them. The
`http_client` unit tests drive a scripted loopback server through stalled,
trickled, redirected, oversized, compressed, short and malformed responses and
TLS with a certificate that `openssl` generates per run; a fake opener raises
every exception type of `open_url`. The `errors` tests run a real
`AnsibleModule` with a synthetic secret, with traceback capture from 2.19 on,
beside a control that shows the leak without `exception=None`. The integration
targets drive test-only consumer modules through real tasks; `exec` needs GNU
coreutils. None of these tests qualifies a product.

If `docs-check` fails with `AF_UNIX path too long` under Python 3.14, run nox
with Python 3.13 (`uv run --python 3.13 noxfile.py`) or from a shorter checkout
path: antsibull-docs starts worker processes whose socket path includes the
checkout.

## Boundaries<a id="boundaries"></a>

This collection was rendered from the `collection_default` variant of
[foundata's Ansible skeletons](https://foundata.com/en/projects/ansible-skeletons/)
and follows the check tooling of the `collection_modules_grid` variant.
Licensing, the changelog configuration, `tests/config.yml`, `.ansible-lint`,
`.yamllint` and `noxfile.py` come from the skeleton, and skeleton changes reach
them as backports. A plugin-only collection differs from the render:

- It has no `roles/`, `FIXME.md`, `requirements.txt` or `extensions/molecule`.
  Molecule needs a target to converge, and a utility collection has none.
- `noxfile.py` leaves out the `import-check` and `molecule` sessions of the
  module skeletons. `import-check` expects modules in the archive, and there is
  no Molecule scenario; `build-import-check` runs the Galaxy importer instead.
- `antsibull-nox.toml` adds the `lint`, `ansible_test_units` and
  `ansible_test_integration_w_default_container` sessions, which `ruff.toml`
  and `.mypy.ini` configure.

`foundata.utils` owns product-independent mechanics and nothing else:

```text
plugins/module_utils/errors.py        exception base, error schema, safe failure
plugins/module_utils/deadline.py      one monotonic budget per operation
plugins/module_utils/http_client.py   bounded byte transport over open_url
plugins/module_utils/exec.py          supervised byte commands and locator records
plugins/doc_fragments/http_client.py  the mechanical connection options
plugins/doc_fragments/exec.py         the mechanical execution locator options
tests/unit/plugins/module_utils/      unit tests and a loopback server
tests/integration/targets/            test-only consumer modules and a server process
```

`http_client` and `exec` import `deadline` and `errors` but not each other, so a
module payload carries only what it uses. What a product decides is
product-owned and stays in the consumer; the README's introduction lists it.

Each helper is published with at least one product consumer that passed its
installed-artifact acceptance with it. A second consumer shows that an
interface is general rather than one product's shape:

- `errors`, `deadline`, `http_client` and the `foundata.utils.http_client`
  fragment serve `foundata.opnsense` and `foundata.zammad`, so they have their
  second consumer.
- `exec` and the `foundata.utils.exec` fragment serve `foundata.zammad`. They
  wait for `foundata.nextcloud` and its `occ` commands as the second consumer.

## Changing a helper<a id="changing-a-helper"></a>

The README's [included content](./README.md#content) lists the supported
symbols. Names with a leading underscore and incidental imports are internal.
Signatures, exceptions, defaults, error codes and fragment options of the listed
symbols are versioned: a change that breaks one needs a new major version, and
the consumers pin `foundata.utils` below 2.0.0.

Add a changelog fragment for every user-visible change, as
[`CONTRIBUTING.md`](./CONTRIBUTING.md#submitting-changes) describes. Before
publication, every consumer that [Boundaries](#boundaries) names qualifies the
release archive, as step 5 of [Releases](#releases) describes.

## Consumers of an unreleased revision<a id="consumers"></a>

A product collection that tests against an unreleased revision of
`foundata.utils` serves it from a tree on `ANSIBLE_COLLECTIONS_PATH`: build this
collection with `ansible-galaxy collection build`, install the archive into a
run-owned path with `ansible-galaxy collection install -p <path>`, and export
`ANSIBLE_COLLECTIONS_PATH=<path>` for the consumer's checks. antsibull-nox
resolves collections through `ansible-galaxy collection list`, so every session,
the `ansible-test` lanes included, sees the copy. Do not put local paths into
`antsibull-nox.toml`. The `import-check` session of a consumer fails until the
`foundata.utils` revision it needs resolves from that path.

## Unit test iteration<a id="unit-test-iteration"></a>

For quick iteration, run the unit tests directly with `pytest` and
ansible-core in one Python environment. The TLS cases need the `openssl`
command, and some `exec` cases use GNU `timeout`. The tests import this
collection as `ansible_collections.foundata.utils`, so link the checkout into a
scratch tree first. From the collection root:

```sh
scratch="$(mktemp -d)"
mkdir -p "${scratch}/ansible_collections/foundata"
ln -s "$(pwd)" "${scratch}/ansible_collections/foundata/utils"
PYTHONPATH="${scratch}" python -m pytest tests/unit -s --import-mode=importlib
```

The `-s` flag prints the measured times of the loopback transport cases. The
`ansible-test-units` lanes remain the reference, because they cover every
admitted ansible-core and Python pair.

## Releases<a id="releases"></a>

Releases require `releasing>=7.0.0` for the `release` command. Install the
development requirement in the maintainer's Python environment:

```sh
python -m pip install -r requirements-dev.txt
```

`release build` runs the committed revision's Ansible sanity tests before
building the archive.

A release of this collection consists of:

- one Semantic Versioning `X.Y.Z` version in [`galaxy.yml`](./galaxy.yml)
- its changelog entry, which antsibull-changelog generates from
  `changelogs/fragments/`
- one `vX.Y.Z` Git tag and one GitHub release for that tag
- the collection archive, published on Ansible Galaxy.

The maintainer needs foundata's `release` command, `ansible-galaxy`,
`antsibull-changelog`, an authenticated `gh`, a Galaxy API token and push
access. `.releasing.toml` declares the release, and `build_ignore` keeps it out
of the archive. foundata's
[collection release procedure](https://github.com/foundata/releasing/blob/main/docs/ansible-collections.md)
explains every `release` command below. The commands need Bash.

Galaxy accepts a version once and never replaces it, so every check after step
4 runs against the archive built there. A helper is published only after at
least one product collection passed its installed-artifact acceptance with it;
[Boundaries](#boundaries) names the consumers.

1. **Run the checks and decide the version.** Every session must pass before
   the release starts.

   ```bash
   version="<CHANGEME version>" # major.minor.patch

   CI=true uv run noxfile.py
   uv run noxfile.py -s ansible-test-sanity
   uv run noxfile.py -s ansible-test-units
   uv run noxfile.py -s ansible-test-integration
   ```

   A change that breaks a versioned interface needs a new major version; see
   [Changing a helper](#changing-a-helper).

2. **Move the version and the changelog to the new release.**

   ```bash
   release version bump "${version}"
   release changelog release "${version}"
   release changelog check --version "${version}"
   release changelog show "${version}"
   ```

   `changelog show` prints the entry that the GitHub release will carry.

3. **Review and commit the release preparation.** The tag will name this
   commit.

   ```bash
   git diff
   git add --all
   git commit -m "release: prepare ${version}"
   git status --short # must print nothing
   ```

4. **Build from the committed revision.**

   ```bash
   dist="../dist-${version}"
   release build --out "${dist}" --expect "${version}"
   sha256sum "${dist}"/*.tar.gz
   ```

   `build` refuses an existing output directory. Remove it before a rebuild.
   `dist` is a fixed path beside the checkout, so after an interruption, set
   `version` and `dist` again in a new shell and continue with the next step.

5. **Qualify the built archive with its consumers.** Install the archive from
   `${dist}` into a fresh collections path and run each consumer's default
   checks, integration targets and Molecule scenario against it, as
   [Consumers of an unreleased revision](#consumers) describes, on the lowest
   supported ansible-core series and on the newest release. The consumers'
   `import-check` sessions resolve the documentation fragments they use from
   that path. Record the archive checksum with the consumer results. A failure
   needs a fix, a new preparation commit and a new build.

6. **Tag the built revision, then push branch and tag.**

   ```bash
   release tag create "${version}" --manifest "${dist}/artifacts.json"
   release push "${version}"
   ```

7. **Publish the validated archive on Galaxy.** Keep the token out of shell
   history and process arguments:

   ```bash
   export ANSIBLE_GALAXY_SERVER_LIST=galaxy
   export ANSIBLE_GALAXY_SERVER_GALAXY_URL=https://galaxy.ansible.com/api/
   printf 'Galaxy API token: '
   read -rs ANSIBLE_GALAXY_SERVER_GALAXY_TOKEN
   printf '\n'
   export ANSIBLE_GALAXY_SERVER_GALAXY_TOKEN
   release publish "${dist}/artifacts.json"
   unset ANSIBLE_GALAXY_SERVER_GALAXY_TOKEN
   ```

8. **Create the GitHub release and verify the publication.**

   ```bash
   release forge release-create "${version}" --manifest "${dist}/artifacts.json"
   release verify "${dist}/artifacts.json"
   ```

   After an interruption,
   `release status "${version}" --manifest "${dist}/artifacts.json"` lists the
   open steps. The procedure's
   [recovery section](https://github.com/foundata/releasing/blob/main/docs/ansible-collections.md#if-something-goes-wrong)
   says which steps can be repeated.
