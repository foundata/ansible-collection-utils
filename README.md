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

- [Included content](#content)
  - [`errors`](#errors)
- [Dependencies](#dependencies)
- [Development and testing](#development)
- [Licensing, copyright](#licensing-copyright)
- [Author information](#author-information)



## Included content<a id="content"></a>

Every helper lives in `plugins/module_utils/` and is imported as
`ansible_collections.foundata.utils.plugins.module_utils.<name>`. Every
public name is an interface of this collection; names with a leading
underscore are internal. Interfaces may still change: nothing is published
yet, and the helpers are changed together with their consumers until the
first publication.


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
