---
name: pin ruff to stop lint CI version drift
overview: The Lint workflow runs ruff-action unpinned with no in-repo ruff config, so it installs whatever ruff is newest at run time. That version floated from 0.15.22 (green) to 0.16.0 (red on 7 pre-existing findings), breaking lint repo-wide. Pin ruff to a known-good version so CI is deterministic and green again; optionally add a ruff config for durable rule selection.
todos:
  - id: pin
    content: Pin version 0.15.22 on both astral-sh/ruff-action steps (check and format --check) in lint.yml
    status: pending
  - id: verify
    content: Verify ruff check + ruff format --check clean locally against the pinned version
    status: pending
  - id: config
    content: "Decide (optional): add a ruff.toml/pyproject config pinning target-version + lint.select, or adopt 0.16.0 rules by fixing the 7 findings"
    status: pending
  - id: pr
    content: Open the fix via the standard PR workflow (human note required; draft PR; gates)
    status: pending
isProject: false
---

# Pin ruff so lint CI stops breaking on version drift

## Problem

[.github/workflows/lint.yml](.github/workflows/lint.yml) runs `astral-sh/ruff-action@v4.0.0` for both `check` and `format --check` (lines 19-26) with **no `version:` input** and **no ruff config** anywhere in the repo. The action logs `Could not find pyproject.toml. Using latest version` and installs whatever ruff is newest at run time. That version floats:

- Jul 22: ruff `0.15.22` -> lint green.
- Today: ruff `0.16.0` -> lint red on 7 pre-existing findings.

`0.16.0` findings from `ruff check .github/scripts`:

- `EXE001` shebang present but file not executable - `label_sync.py:1`, `pr_labeler.py:1`
- `ISC004` implicit string concat in a collection - `pr_labeler.py:313`
- `S112` `try`/`except`/`continue` without logging - `pr_labeler.py:391`
- `BLE001` blind `except Exception` - `pr_labeler.py:391`
- `I001` import block unsorted - `test_pr_labeler.py:16`
- `RUF100` unused `noqa: E402` (0.16 no longer fires E402 after a `sys.path.insert`, so the directive is now dead) - `test_pr_labeler.py:16`

None come from recent feature work. They surface only because the default ruleset drifted between releases. Confirmed clean under `0.15.22` and `0.6.9`; red under `0.16.0`.

## Recommended fix: pin the ruff version (deterministic, no source churn)

Add `version: "0.15.22"` to both ruff-action steps. Restores green immediately and makes CI reproducible - the ruleset stops changing out from under the repo.

```yaml
      - uses: astral-sh/ruff-action@v4.0.0
        with:
          version: "0.15.22"
          src: '.github/scripts'
          args: 'check'
      - uses: astral-sh/ruff-action@v4.0.0
        with:
          version: "0.15.22"
          src: '.github/scripts'
          args: 'format --check'
```

## Alternative (or follow-up): adopt 0.16.0's stricter rules

`0.16.0`'s new findings are mostly reasonable. To move forward instead of pinning back:

- `chmod +x .github/scripts/*.py` (or drop the shebangs) for `EXE001`.
- Parenthesize the implicit concat at `pr_labeler.py:313` for `ISC004`.
- Narrow the `except` at `pr_labeler.py:391`, or add a justified `# noqa: BLE001,S112`.
- Re-sort imports and drop the dead `noqa: E402` in `test_pr_labeler.py:16` for `I001`/`RUF100`.
- Then pin to `0.16.0`.

Pinning is required either way; unpinned drifts again on the next release.

## Even more robust (optional): add an in-repo ruff config

Add `ruff.toml` (or `[tool.ruff]` in `pyproject.toml`) pinning `target-version` and an explicit `lint.select`, so the ruleset is defined in-repo and does not track the tool's evolving defaults. Composes with a pinned action version.

## Verification

- Locally at the pinned version: `ruff check .github/scripts` and `ruff format --check .github/scripts` clean.
- On the PR: the `Python (ruff)` check goes green.

## Interaction with the open labeler PR

The labeler PR is currently red only on this ruff drift. Once this pin lands on `main`, rebase that PR onto updated `main`; its own `pull_request` lint run then uses the pinned version and goes green. No labeler code change is needed for that check.

## PR mechanics

New branch off `main`, single-file change to `lint.yml`. Standard PR workflow with the mandatory human note before creating the PR.
