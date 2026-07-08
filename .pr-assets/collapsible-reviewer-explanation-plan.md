---
name: Collapsible reviewer explanation
overview: Make the CODEOWNERS reviewer-explanation section (sticky comment and optional PR-body region) collapsible by keeping a visible purple [!IMPORTANT] callout and moving the verbose per-team file/pattern breakdown into a default-collapsed <details> block. Rendering-only change in pr_labeler.py plus test updates.
todos:
  - id: render
    content: "Restructure render_domain_reasons() in pr_labeler.py: keep top-level [!IMPORTANT] callout with count sentence + provenance visible; move per-team file/pattern breakdown into a sibling default-collapsed <details> block (blank line after <summary>, plain non-blockquoted markdown inside). Add team pluralization helper. Preserve leading_rule semantics."
    status: pending
  - id: update-tests
    content: Update existing render_domain_reasons tests in test_pr_labeler.py to expect plain (non-blockquoted) team/file lines and the new callout wording; keep leading_rule and marker/idempotency tests.
    status: pending
  - id: add-tests
    content: "Add tests: <details> is default-collapsed with team count in summary; file lines inside details and absent from callout blockquote; blank line after </summary>; 1-team vs N-team pluralization; new-shape render/upsert idempotency."
    status: pending
  - id: verify
    content: Run ruff check, ruff format --check, and pytest on .github/scripts; optionally paste rendered output into a scratch comment to confirm the callout stays purple and the section collapses.
    status: pending
isProject: false
---

# Make the CODEOWNERS reviewer-explanation section collapsible

## Goal

Reduce the noise of the reviewer-explanation section (added in commit `299e7c8`) while preserving the distinct purple callout. Keep a short `> [!IMPORTANT]` header always visible and collapse the long per-team file/pattern breakdown into a default-collapsed `<details>` block, mirroring the thog precedent ([Add Integrations team PR checklist comment](https://github.com/trufflesecurity/thog/pull/6703)).

## Key constraint

GitHub alerts do not render inside `<details>` — a `> [!IMPORTANT]` nested in a `<details>` degrades to a plain gray blockquote ([github/markup #1753](https://github.com/github/markup/issues/1753), [community #118296](https://github.com/orgs/community/discussions/118296)). So the callout stays a top-level blockquote and the `<details>` sits as a sibling below it.

## Scope

Rendering-only. All plumbing stays as-is: the `DOMAIN_REVIEW_REASONS:START/END` markers are unchanged, so `upsert_managed_section` (body), `find_reasons_comment` / `plan_comment_action` / `apply_comment` (comment), idempotency, and the CRLF/whitespace normalization all keep working. No workflow change — [`.github/workflows/pr-labeler-reusable.yml`](.github/workflows/pr-labeler-reusable.yml) only passes the `reasons_target` input.

## The one change: `render_domain_reasons()`

Restructure the output in [`.github/scripts/pr_labeler.py`](.github/scripts/pr_labeler.py) (lines 264-310). Today the whole section is one blockquote with team + file lines inside the callout. New shape:

- **Visible callout (top-level blockquote, still renders purple):** heading, a one-line count sentence (`N team(s) own some of the files changed here, per CODEOWNERS. Expand below for the files that matched.`), and the `<sub>Maintained automatically...</sub>` provenance line (kept visible so authors know not to hand-edit it, per the reviewer note on the original PR).
- **Blank line**, then a **top-level `<details>`** (default-collapsed): `<summary>` shows `Which teams and files (N)`, a blank line, then the existing per-team markdown list (`- **slug** - charter` / `  - \`file\` (matched \`pattern\`)` / `  - ...and N more`) as plain, non-blockquoted markdown, then a blank line before `</details>`.

`leading_rule` semantics are unchanged: body keeps the leading `---`, comment omits it. The `_ordered_reason_slugs`, `_code_span`, file cap, sorting, and charter logic are all reused verbatim — only where the lines are emitted (inside details, un-prefixed) and the callout wording change. Add a small pluralization helper for `team`/`teams`.

### Example — comment variant (`leading_rule=False`)

```markdown
<!-- DOMAIN_REVIEW_REASONS:START -->
> [!IMPORTANT]
> **Why these teams were requested for review**
> 2 teams own some of the files changed here, per [CODEOWNERS](https://github.com/org/repo/blob/HEAD/.github/CODEOWNERS). Expand below for the files that matched.
> <sub>Maintained automatically by the PR labeler.</sub>

<details>
<summary>Which teams and files (2)</summary>

- **scanning** - scan engine, job control, reverification
  - `pkg/engine/scan.go` (matched `/pkg/engine/`)
- **eng-leads** - engineering leads; default reviewers for files no domain team owns
  - `README.md` (matched `*`)

</details>
<!-- DOMAIN_REVIEW_REASONS:END -->
```

### Example — body variant (`leading_rule=True`)

Identical, but with `---` and a blank line inserted right after the `START` marker (unchanged behavior), separating the block from the author's text.

## Tests: update + add in [`.github/scripts/test_pr_labeler.py`](.github/scripts/test_pr_labeler.py)

Update assertions that assumed team/file lines are blockquoted (they are now plain markdown inside `<details>`):

- `test_structure_markers_and_callout` (line 601): drop the "every content line starts with `>`" assertion; instead assert the callout lines are blockquoted, and that `<details>`, `<summary>`, `</details>` are present with team lines un-prefixed.
- `test_known_slug_shows_charter_no_label` (614), `test_eng_leads_charter_present` (623), `test_unknown_slug_bare_name_no_charter` (630): change `"> - **slug**"` expectations to `"- **slug**"`.
- `test_deterministic_known_first_catch_all_last` (640): filter on `"- **"` instead of `"> - **"`.
- `test_file_cap_and_more` (652): filter file lines on `"  - \`"` and expect `"  - ...and 2 more"` (no `>` prefix).
- `test_codeowners_plain_when_no_url` (673): update the wording assertion to the new count sentence (still `"CODEOWNERS"`, still no `](http`).
- `test_leading_rule_present_by_default` (680) and `test_leading_rule_omitted_for_comment` (689): still valid — callout remains first after `START` (comment) / after `---` (body); keep as-is.
- `test_backtick_in_path_neutralized`, `test_codeowners_link_when_url_provided`, all `upsert_managed_section` / comment-plumbing / `test_render_then_upsert_idempotent` tests: unchanged (substring/marker-based).

Add new tests:

- `<details open` is NOT used (default-collapsed) and `<summary>` reflects the team count.
- Team/file breakdown lines live inside the `<details>` and are not blockquoted; the callout blockquote does not contain the file lines.
- A blank line follows `</summary>` (so the inner markdown renders) and separates the callout from `<details>`.
- Pluralization: `1 team` vs `2 teams`.
- End-to-end idempotency with the new shape (render -> upsert -> render -> upsert is a no-op) — extend/confirm `test_render_then_upsert_idempotent`.

## Verification

- `ruff check .github/scripts` and `ruff format --check .github/scripts`.
- `pytest .github/scripts/test_pr_labeler.py`.
- Manual GitHub render sanity check (optional): paste the rendered comment into a scratch PR/comment to confirm the callout stays purple and the `<details>` collapses.

---

## PR workflow

### Human note (verbatim - do not edit)

> We're starting to have a lot of information all pushed to first order retrievability in these PRs.  Let's push it down to second order retrievability (a single click) so the information is discoverable but not clutter

### Context detected

- **Implementation state:** pre-implementation (branch `main`, clean tree, 0 commits ahead of `origin/main`) -> B2 worktree + B3 implementation run.
- **Repo:** `trufflesecurity/.github` (PUBLIC). Reference-locality: no bare ticket keys anywhere; links only.
- **GitHub user:** `martinlocklear`.
- **Tracker ticket:** none found (Linear search returned only unrelated tickets), consistent with the parent PR #19 having none. `Tracked in` line omitted; A1a ticket claim skipped.
- **Quality gates:** no Makefile; Python-only. Run `ruff check .github/scripts`, `ruff format --check .github/scripts`, `pytest .github/scripts/test_pr_labeler.py`, plus the advisory `martin-lint-local-changes` sweep on the diff.
- **Team checklist (A3a):** no-op. This repo has no CODEOWNERS and no team-checklist mechanism, so no team checklist applies.
- **Commit style:** plain-English imperative, no conventional-commit prefix. Note: the parent PR's commits used `feat:`/`test:` prefixes, but the always-on minimum forbids them; flagging this deviation from local history.

### Steps

- **Branch:** `martinlocklear/cursor/collapsible-reviewer-explanation`
- **Worktree:** yes, `/tmp/github-collapsible-reviewer-explanation`
- **pr-assets plan doc:** yes -> `.pr-assets/collapsible-reviewer-explanation-plan.md`
- **Quality gates:** ruff check, ruff format --check, pytest (above) + advisory lint sweep
- **Review panel (B4c):** run `martin-convene-review-panel` on the diff before PR creation; resolve blocker/should-fix findings and fold predicted-reviewer-question rationale into the PR body / comments
- **Commit strategy:** single commit (small, cohesive rendering + test change)
- **Conversation summary comment:** yes (draft below)
- **Example render comment (B8a, user-directed):** yes. After the PR exists, post a PR comment showing the actual rendered output of the new collapsible section, generated from the real `render_domain_reasons()` on sample reasons, so reviewers can see how the collapsed callout + `<details>` renders on GitHub. Explicitly requested by Martin, so authorized to post.
- **Babysit (B9):** yes, reuse the implementation worktree

### PR description outline (generated body - martin-voice + martin-prose)

Title: `Make the CODEOWNERS reviewer-explanation section collapsible`

```markdown
[authored by Cursor]

## Summary

- Keep the `[!IMPORTANT]` reviewer-explanation callout visible (headline, team count, provenance) and move the per-team file/pattern breakdown into a default-collapsed `<details>` block.
- Applies to both publish targets (the sticky comment and the optional PR-body region). The `DOMAIN_REVIEW_REASONS` markers, comment/body plumbing, and idempotency guard are unchanged; this is a rendering-only change.

## Motivation

These PRs now carry a lot of always-visible automated output. This pushes the verbose file/pattern breakdown one click down so it stays discoverable without cluttering the PR. The callout has to stay outside the collapsed region because GitHub does not render alert callouts inside `<details>` (they degrade to plain blockquotes).

**Background:** [Collapsible reviewer explanation plan](<PLAN_DOC_URL>)

## Test plan

- [ ] `ruff check .github/scripts` and `ruff format --check .github/scripts`
- [ ] `pytest .github/scripts/test_pr_labeler.py`
- [ ] CI checks green
```

(Test-plan checkboxes filled with actual results at B7 after B4 runs.)

### Conversation summary (draft)

```markdown
[authored by Cursor]

## Conversation summary

This PR originated from a request to reduce clutter in the automated PR sections by pushing the CODEOWNERS reviewer-explanation detail from always-visible ("first order") to one-click-away ("second order").

**Plan doc:** [Collapsible reviewer explanation](<PLAN_DOC_URL>)

### Key decisions

- **Callout stays visible, breakdown collapses.** The purple `[!IMPORTANT]` callout (headline, team count, and the "maintained automatically" provenance) remains first-order; the per-team file/pattern list moves into a default-collapsed `<details>`.
- **Callout kept outside the collapsed region.** GitHub does not render alert callouts inside `<details>` (they degrade to plain blockquotes), so the callout sits as a top-level sibling above the `<details>` rather than inside it.
- **Rendering-only.** The `DOMAIN_REVIEW_REASONS` markers, comment/body publish paths, and the "edit only on change" idempotency guard are untouched, so both publish targets keep working with no churn.

### Alternatives considered

- **Single `<details>`, drop the alert (thog-style).** Fully collapses everything under one summary but loses the purple callout that distinguishes this section from Bugbot's note.
- **Collapse only the file lists, keep team names visible.** Most information at a glance, but the busiest markup; not chosen, for simplicity.
```

### Example render comment (draft, B8a)

Posted as a separate PR comment after creation. The fenced block is filled at B8a with the real output of `render_domain_reasons()` on sample reasons (comment variant), so reviewers see the exact GitHub rendering.

```markdown
[authored by Cursor]

## Example render

Here is how the new collapsible reviewer-explanation section renders (comment variant). The `[!IMPORTANT]` callout stays visible; the per-team file/pattern breakdown is one click away.

<!-- rendered output of render_domain_reasons(sample, leading_rule=False) inserted here -->
```