# trufflesecurity/.github

Org-wide defaults and shared automation for TruffleHog repositories.

## What lives here

- **`.github/PULL_REQUEST_TEMPLATE.md`** — default PR template inherited by any
  repo in the org that does not define its own. Repos can ship their own
  template at the same path to extend or replace it.
- **`labels.yml`** — single source of truth for the standard label taxonomy
  (size/risk/review/status/complexity). Synced into every consumer repo by the
  reusable label-sync workflow on a daily cron.
- **`.github/workflows/pr-labeler-reusable.yml`** — reusable workflow that
  applies size/risk/checkbox labels to PRs. Called from each consumer repo's
  `.github/workflows/pr-labeler.yml`.
- **`.github/workflows/stale-reusable.yml`** — reusable workflow wrapping
  `actions/stale` with the org's PR hygiene policy (14-day stale, 16-day
  close, exempt `review/urgent` and drafts). Called from each consumer's
  `.github/workflows/stale.yml`.
- **`.github/workflows/label-sync-reusable.yml`** — reusable workflow that
  reads `labels.yml` and applies it to its caller repo via
  `gh label create --force`. Idempotent and additive.
- **`.github/scripts/`** — Python scripts powering the reusable workflows.
  Unit-tested via `.github/workflows/test-scripts.yml`.

## How to add or change a label

Edit `labels.yml`. The next scheduled run of each consumer's `Sync Labels`
workflow propagates the change (midnight UTC daily). To propagate immediately,
trigger the workflow on each consumer repo via:

```bash
gh workflow run sync-labels.yml --repo trufflesecurity/<repo>
```

The current list of consumer repos is maintained in our internal rollout doc
(see the PR Labeling & Hygiene plan).

Also update the org-level **Settings > Repository defaults > Repository labels**
list so brand-new repos get the same set on day one.

## Permissions model for consumer workflows

Permissions are declared in caller workflows and inherited by these reusable
workflows via `GITHUB_TOKEN`. Do **not** add `permissions:` blocks to the
reusable workflows — they would override the caller's grant and surface as
"Resource not accessible by integration" failures.

| Reusable workflow | Permission required |
| --- | --- |
| `pr-labeler-reusable.yml` | `pull-requests: write` |
| `label-sync-reusable.yml` | `issues: write` (labels are an issue resource) |
| `stale-reusable.yml` | `pull-requests: write` and `issues: write` |

## Versioning

Caller workflows reference these reusables at `@main`. Pushes to this repo's
`main` branch immediately affect all consumer repos. Branch protection on
`main` requires PR review before merging workflow changes.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install pytest pyyaml
.venv/bin/python -m pytest .github/scripts -v
```
