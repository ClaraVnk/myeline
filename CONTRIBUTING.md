# Contributing to Myeline

Thanks for your interest in improving Myeline! Bug reports, fixes, new
connectors, documentation and translations are all welcome. This guide gets you
from a fresh clone to a green pull request.

By contributing you agree that your contributions are licensed under the
project's [AGPL-3.0](LICENSE).

---

## Code of conduct

Be respectful and constructive. We follow the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/). Harassment of any
kind is not tolerated.

---

## Development setup

Myeline targets **Python 3.13** (matching the runtime image and CI).

```bash
git clone https://github.com/ClaraVnk/myeline.git
cd myeline

python3.13 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
```

You do **not** need the full container stack to develop or run tests — the test
suite runs against an in-memory SQLite database.

To run the full app locally with all services (MariaDB, Redis, Ollama), use the
development compose file, which builds from your checkout:

```bash
cp .env.example .env   # fill in the required secrets — see the README
podman-compose -f docker-compose.dev.yml up --build
```

---

## Quality gates

These mirror the CI workflow (`CI & Quality Gates`). Run them before opening a
PR; the same checks must pass for it to be merged.

```bash
# Tests (parallel; no containers required)
pytest tests/ -n auto

# Coverage report → htmlcov/index.html
pytest tests/ --cov=app --cov-report=html

# Lint
flake8 --config=./config/setup.cfg .

# Import ordering (check only)
isort --settings-path config/setup.cfg --check-only .

# Security static analysis
bandit -c bandit.yaml -r . -n 10 --severity-level medium
```

To auto-fix import order before committing: `isort --settings-path config/setup.cfg .`
Code formatting follows `black` defaults (`black .`).

A change is **done** only when it compiles, the formatter has run, lint passes
with no new warnings, the relevant tests pass, and — for UI changes — it has been
verified in a browser.

---

## Commit conventions

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short imperative description>
```

Common types: `feat`, `fix`, `chore`, `docs`, `refactor`, `perf`, `test`,
`build`, `ci`, `style`, `revert`. Mark breaking changes with `!` (e.g. `feat!:`)
or a `BREAKING CHANGE:` footer.

Examples:

```
feat(connectors): add Zotero group library support
fix(rag): handle empty retrieval result without 500
docs: clarify CLOUD_TOKEN_KEY rotation steps
```

Keep commits atomic — each commit should compile and pass tests. All code,
identifiers, comments and commit messages are in **English**.

---

## Pull request process

1. Branch from `synapse` using a descriptive name: `feat/...`, `fix/...`,
   `chore/...`, `docs/...`.
2. Keep PRs small and single-purpose. If a diff grows past ~400 significant
   lines, consider splitting it.
3. Make sure all quality gates above pass locally.
4. Write a clear PR description covering **Context**, **Changes**, **Tests** and
   **Risks**.
5. Link any related issue.

A maintainer will review; please be responsive to feedback. Thank you!

---

## Reporting bugs & requesting features

Open an issue using the provided templates. For **security vulnerabilities**, do
**not** open a public issue — email **hello@myeline.io** (see
[docs/SECURITY.md](docs/SECURITY.md)).
