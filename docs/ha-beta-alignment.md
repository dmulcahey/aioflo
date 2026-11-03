# Home Assistant beta alignment

## Compatibility warning

**Minimum Python changes from 3.11.0 to 3.14.2.** This drops Python 3.11,
3.12, 3.13, and 3.14.0–3.14.1. It affects every consumer, not just Home
Assistant. Older installations must retain a compatible aioflo release.
The metadata has no upper Python bound, as before; testing covers CPython
3.14.2 and 3.14.10, not future Python versions or PyPy. Retaining the existing
PyPy classifier is not evidence of a tested 3.14 PyPy implementation.

aiohttp changes from `>=3.14.4` to `==3.14.5`, as explicitly requested for
beta alignment. This deliberately narrows library interoperability: applications
requiring another aiohttp version will have a resolver conflict. Subsequent HA
beta/stable upgrades need another reviewed dependency update.

## Immutable baseline and sources

Verified on 2026-11-03. Fetched `dmulcahey/aioflo` dev and checked it equals
`1731a75dfbe652024d0a335f562996b97162b1e4` before branching. The initial
preloaded checkout was older; this report and all changes use the fetched base.
No aioflo AGENTS.md exists; CONTRIBUTING.md requests setup, tests, coverage,
and documentation. The timezone/authentication branches are independent.

[HA releases](https://github.com/home-assistant/core/releases) identifies
[2026.11.0b4](https://github.com/home-assistant/core/releases/tag/2026.11.0b4)
as the newest published beta, released **2026-11-03 13:48 UTC**. Its tag resolves
via `git ls-remote` and a fetched tag to
`ab3bdecda0c2fffb94a5f02d460842113bd0d4d9`. All HA references below use that
immutable commit, not dev, rc branch head, or the latest stable release.

- [HA metadata](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/pyproject.toml): Python `>=3.14.2`, runtime aiohttp `3.14.5`, setuptools build backend `84.0.0`.
- [HA runtime minimum](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/homeassistant/const.py): `REQUIRED_PYTHON_VER = (3, 14, 2)`.
- [HA checkout runtime](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/.python-version): `3.14.10`.
- [HA CI](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/.github/workflows/ci.yaml): reads `.python-version`; additional Python versions are empty.
- [Python 3.14.10 release](https://www.python.org/downloads/release/python-31410/): released October 25, 2026. This is a released maintenance runtime, not an invented patch number. The initially installed uv lacked it; updated uv downloaded it successfully.
- [HA test tools](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/requirements_test.txt): pytest, pytest-asyncio, pytest-cov, coverage, mypy and prek pins.
- [HA hooks](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/.pre-commit-config.yaml) and [generated hook requirements](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/requirements_test_pre_commit.txt).
- [HA package constraints](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/homeassistant/package_constraints.txt): restrictions on installed packages, not a declaration that aioflo should depend on them.
- [HA Flo consumer manifest](https://github.com/home-assistant/core/blob/ab3bdecda0c2fffb94a5f02d460842113bd0d4d9/homeassistant/components/flo/manifest.json): currently requires `aioflo==2026.9.4`. This branch does not publish a new version or update that manifest.

## Complete direct dependency and tool inventory

“Unchanged” means no applicable exact pin was found in the beta's metadata,
runtime/test requirements, package constraints, or hook configuration. Retaining
those declarations is the conservative recommendation; arbitrary latest-PyPI
updates would not be HA beta alignment. Resolved versions are test observations,
not invented HA pins. No new lint engine or packaging backend is introduced.

| Item | Before | After | Applicable HA evidence / decision |
| --- | --- | --- | --- |
| Python minimum | `>=3.11.0` | `>=3.14.2` | HA metadata and runtime check |
| Python checkout pin | absent | `3.14.10` | HA `.python-version` |
| aiohttp (only direct runtime package) | `>=3.14.4` | `==3.14.5` | HA metadata, generated runtime requirements and constraints agree |
| poetry-core (build backend) | `>=1.0.0` | unchanged | No analogous HA pin; HA uses setuptools, a different backend. Observed build: 2.5.0 |
| aresponses | Poetry `^2.0.0`; test file `>=2.0.0` | Poetry unchanged; test file `>=2.0.0,<3.0.0` | No HA pin; make both install paths agree on existing 2.x policy. Observed 2.1.6 |
| filelock | `>=3.20.3` | unchanged | No applicable HA pin; retain existing floor. Observed 4.2.7 |
| pre-commit | `^4.0.0` | unchanged | HA uses prek 0.5.7, a different runner. Retain pre-commit 4.x; observed 4.7.0 |
| pytest | Poetry `^9.0.3`; test file `>=9.0.3` | `==9.0.3` in both | HA test requirements |
| pytest-asyncio | Poetry `^0.19.0`; test file `>=0.19.0` | `==1.4.0` in both | HA test requirements; local fixture adaptation required |
| pytest-cov | Poetry `^3.0.0`; test file `>=3.0.0` | `==7.1.0` in both | HA test requirements |
| coverage | transitive, unpinned | `==7.16.2` in both | Explicit HA test-tool pin; keeps coverage tool aligned instead of resolver-selected 7.17.1 |
| virtualenv | `>=21.14.9` | unchanged | No applicable HA pin; retain existing floor. Observed 21.15.3 |
| bandit hook | 1.9.4 | unchanged | No HA bandit hook/pin |
| bandit extra: pbr | unpinned | unchanged | No applicable HA pin |
| black hook | 26.5.1 | unchanged | HA uses Ruff format; no analogous Black pin |
| codespell hook | v2.4.3 | unchanged | Already matches HA |
| flake8 hook | 7.3.0 | unchanged | HA uses Ruff lint; no analogous Flake8 pin |
| flake8 extras | flake8-bugbear, flake8-docstrings, pydocstyle unpinned | unchanged | No applicable HA pins |
| isort hook | 9.0.1 | unchanged | HA uses Ruff import sorting; no analogous isort pin |
| isort extra: toml | unpinned | unchanged | No applicable HA pin |
| mypy hook | v2.3.1 | v2.4.0 | HA test requirements; preserve existing mirror hook mechanism |
| mypy extras: ast-serialize, librt | resolver-selected (review observed 0.13.1 and 0.16.0) | `ast-serialize==0.12.1`, `librt==0.16.0` in isolated hook | Explicit internal mypy dependency pins in HA test requirements |
| pre-commit-hooks | v6.0.0 | unchanged | Already matches HA; JSON/YAML/whitespace/branch checks retained |
| pydocstyle hook | 6.3.0 | unchanged | No applicable HA pin; do not confuse Ruff's pydocstyle rules with this package |
| shellcheck hook wrapper | gruntwork-io/pre-commit v0.1.30 | unchanged | No HA counterpart; system shellcheck executable was not version-pinned |
| pyupgrade hook | v3.21.2, `--py311-plus` | unchanged | No HA counterpart; existing syntax baseline remains valid on 3.14; unnecessary syntax churn avoided |
| Poetry CLI (setup/publish/release scripts) | unpinned | unchanged | No installable HA CLI pin. HA's `poetry==1000000000.0.0` is an intentional runtime exclusion, not a version to install |
| Code Climate engines | duplication, fixme, radon enabled, unpinned | unchanged | External service configuration; no matching HA pins |
| pylint configuration | pylintrc only; no installed dependency | unchanged | HA pins pylint 4.1.3, but aioflo does not invoke it in setup/CI/hooks; do not introduce it merely because a config exists |
| pip/venv | interpreter-provided setup tools | unchanged | No matching direct beta tool pins applied |

pytest-aiohttp is **not** a dependency at the verified aioflo base (it was in
the older preloaded checkout). HA pins it to 1.1.1, but it is not needed here.
HA's setuptools 84.0.0, Ruff 0.17.3 and prek 0.5.7 are not pins for aioflo's
Poetry backend, Black/Flake8/isort hooks, or pre-commit runner respectively.

Other transitive packages remain resolver-managed, as before. This is not a
claim that aioflo's whole environment equals HA's huge dependency graph. Shared
runtime dependencies resolved in testing include attrs 26.1.0, propcache 0.5.4
and yarl 1.25.1, matching explicit HA runtime pins. No unrelated HA integration
constraints are promoted into aioflo's public dependency metadata.

## Implementation and validation

- Classifiers drop Python 3.11–3.13. CI tests the minimum and exact checkout
  runtime and now installs the package itself as well as test requirements, so
  packaging incompatibilities cannot hide behind source-directory imports.
- Coverage, lint and publish workflows use `.python-version`. Existing checkout
  v5, setup-python v6, codecov v5, pre-commit/action v3.0.1 and release-drafter v5
  are retained; these service actions are not Python package pins.
- Existing retired 3.9/3.10 compatibility jobs remain explicit no-op status checks.
  The test job names change to `Tests (3.14.2)` and `Tests (3.14.10)`; repository
  branch protection must be checked by the owner before a future PR/merge.
- The aresponses fixture override uses its public ResponsesMockServer context
  manager inside a pytest-asyncio fixture. This removes dependence on the removed
  event_loop fixture without overriding pytest's loop lifecycle or upgrading an
  unmapped dependency. Function-scoped async fixture loops are explicit.
- No production Python source, API behavior, authentication, timezone handling,
  or device interactions change. Existing syntax is valid on the new runtime.
- There is no tracked lockfile; poetry.lock is intentionally ignored. No lockfile
  or generated file was hand-edited. Poetry install generated an ignored local
  lockfile for validation; it is not part of the change. HA generated requirements
  were only read.
- All 51 existing mocked tests pass on CPython 3.14.2 and 3.14.10, with 100%
  statement coverage (273 statements). The suite also passed on 3.14.5 before
  coverage was pinned. No live device/service requests were used.
- Poetry 2.5.1 successfully resolves/installs the declared development environment;
  poetry check succeeds with warnings about the existing legacy metadata/dev
  dependency format. These are retained to avoid an unrelated packaging migration.
- Source distribution and wheel builds succeed with poetry-core 2.5.0. All 51
  tests also pass against the installed wheel outside the source repository.
  Wheel metadata declares Python >=3.14.2 and only aiohttp ==3.14.5 as a runtime
  requirement. A Python 3.13.5 dry-run install fails with the expected minimum
  Python conflict. pip dependency consistency checks pass.
- Poetry automatically enriches Python classifiers, including 3.15 for the
  open-ended minimum. That generated classifier does not constitute 3.15 testing;
  the explicit support/test policy remains as stated above.
- All 14 configured pre-commit hooks pass, including mypy 2.4.0. The cloud
  image lacked shellcheck; a local ShellCheck 0.11.0 binary completed the existing
  shellcheck hook. This validation binary is not an HA pin or a repository change.
- Twelve aresponses deprecation warnings remain per run after the review test
  improvements (eleven in the initial candidate): asyncio.iscoroutinefunction
  is scheduled for removal in Python 3.16. A later Python support expansion needs
  revalidation or an independently justified mock-library update.

This candidate requires independent review before being considered ready for a
PR. No release version bump, PR, merge, deployment or device operation is part
of this change.

## Independent review follow-up

The initial candidate was `3f27fe871b7ec47af982cb936327e33b28dc402f`.
Independent review identified that the isolated mypy hook still resolved
ast-serialize 0.13.1 despite HA explicitly pinning 0.12.1. Hook
additional_dependencies now constrain ast-serialize 0.12.1 and librt 0.16.0,
matching HA's test requirements alongside mypy 2.4.0. These are hook-environment
constraints, not new aioflo runtime dependencies. Inspection of the rebuilt
isolated hook environment confirms Python 3.14.10, mypy 2.4.0, ast-serialize
0.12.1 and librt 0.16.0; the mypy hook and all 14 hooks pass.

Review also exposed two pre-existing false-positive tests. The system mode test
now registers the valid sleep-to-away request with an exact payload matcher,
requires success, and verifies that all expected requests were matched in order.
The legacy 401 test supplies repeatable 401 responses and explicitly asserts one
request, then checks that no unexpected requests occurred. A missing mock can no
longer satisfy the intended assertion by merely producing another RequestError.

Three isolated production-code mutations fail the revised tests as expected:
rejecting the valid away revert mode; sending home instead of away in the payload;
and retrying the legacy 401 once. Mutation copies were outside the repository;
no production changes are included. The unchanged implementation passes all
51 tests on both Python 3.14.2 and 3.14.10 with 100% statement coverage.
