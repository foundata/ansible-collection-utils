# Ansible collection: `foundata.utils`

This repository contains the `foundata.utils` Ansible Collection: shared
transport and safety mechanics for foundata's product collections. It ships
module utilities and a documentation fragment only. There are no modules,
roles or playbooks; a product collection imports the utilities into its own
modules.

The collection depends on ansible-core alone and supports the same floors as
its consumers: ansible-core 2.19 or newer on the controller, Python 3.9 or
newer where a module runs.



## Table of contents<a id="toc"></a>

- [Dependencies](#dependencies)
- [Development and testing](#development)
- [Licensing, copyright](#licensing-copyright)
- [Author information](#author-information)



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
Copyright (c) 2026 foundata GmbH

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
a
[SPDX software bill of materials (SBOM)](https://en.wikipedia.org/wiki/Software_Package_Data_Exchange).
<!--REUSE-IgnoreEnd-->



## Author information<a id="author-information"></a>

This project was created and is maintained by foundata GmbH.

Initially based on an
[Ansible skeleton](https://foundata.com/en/projects/ansible-skeletons/)
developed by [foundata](https://foundata.com/).
