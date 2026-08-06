"""Tests for pr_labeler module.

Run with: python -m pytest .github/scripts/test_pr_labeler.py -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import pr_labeler  # noqa: E402


# ---- size_bucket -----------------------------------------------------------


class TestSizeBucket:
    def test_zero_or_negative_returns_none(self):
        assert pr_labeler.size_bucket(0) is None
        assert pr_labeler.size_bucket(-1) is None

    def test_xs_boundary(self):
        assert pr_labeler.size_bucket(1) == "size/XS"
        assert pr_labeler.size_bucket(10) == "size/XS"

    def test_s_boundary(self):
        assert pr_labeler.size_bucket(11) == "size/S"
        assert pr_labeler.size_bucket(50) == "size/S"

    def test_m_boundary(self):
        assert pr_labeler.size_bucket(51) == "size/M"
        assert pr_labeler.size_bucket(250) == "size/M"

    def test_l_boundary(self):
        assert pr_labeler.size_bucket(251) == "size/L"
        assert pr_labeler.size_bucket(999) == "size/L"

    def test_xl_starts_at_1000(self):
        assert pr_labeler.size_bucket(1000) == "size/XL"
        assert pr_labeler.size_bucket(50_000) == "size/XL"


# ---- risk_from_body --------------------------------------------------------


def _plan() -> pr_labeler.LabelPlan:
    return pr_labeler.LabelPlan(pr_number=1)


class TestRiskFromBody:
    def test_no_marker_returns_none(self):
        plan = _plan()
        assert pr_labeler.risk_from_body("nothing here", plan) is None
        assert plan.notes == []

    def test_low_risk(self):
        body = "<!-- CURSOR_SUMMARY -->\n**Low Risk** assessment OK"
        assert pr_labeler.risk_from_body(body, _plan()) == "risk/low"

    def test_medium_risk(self):
        body = "<!-- CURSOR_SUMMARY -->\nthings\n**Medium Risk** detected"
        assert pr_labeler.risk_from_body(body, _plan()) == "risk/medium"

    def test_high_risk(self):
        body = "<!-- CURSOR_SUMMARY -->\n**High Risk** is here"
        assert pr_labeler.risk_from_body(body, _plan()) == "risk/high"

    def test_case_insensitive(self):
        body = "<!-- CURSOR_SUMMARY -->\n**HIGH risk** seen"
        assert pr_labeler.risk_from_body(body, _plan()) == "risk/high"

    def test_unmapped_level_falls_back_to_high_with_warning(self):
        plan = _plan()
        body = "<!-- CURSOR_SUMMARY -->\n**Critical Risk** detected"
        assert pr_labeler.risk_from_body(body, plan) == pr_labeler.RISK_FALLBACK
        assert any("unmapped" in note for note in plan.notes)

    def test_marker_present_no_match_warns(self):
        plan = _plan()
        body = "<!-- CURSOR_SUMMARY -->\nNo risk verbiage at all"
        assert pr_labeler.risk_from_body(body, plan) is None
        assert any("regex did not match" in note for note in plan.notes)

    def test_text_before_marker_ignored(self):
        body = "**Low Risk** appears before\n<!-- CURSOR_SUMMARY -->\n**High Risk**"
        assert pr_labeler.risk_from_body(body, _plan()) == "risk/high"


# ---- field_state -----------------------------------------------------------


def _urgent_state(body: str) -> str | None:
    return pr_labeler.field_state(
        body,
        yesno=pr_labeler.URGENT_YESNO_REGEX,
        checkbox=pr_labeler.URGENT_CHECKBOX_REGEX,
    )


def _complexity_state(body: str) -> str | None:
    return pr_labeler.field_state(
        body,
        yesno=pr_labeler.COMPLEXITY_YESNO_REGEX,
        checkbox=pr_labeler.COMPLEXITY_CHECKBOX_REGEX,
    )


class TestFieldStateYesNo:
    """Current template format: ``- **Field** (...): yes|no``."""

    @pytest.mark.parametrize(
        "body",
        [
            "- **Urgent** (needs same-day review): yes",
            "- **Urgent**: yes",
            "* **urgent**: YES",
            "- **Urgent** (needs same-day review): yes, plus extra context",
            "-   **Urgent**   (needs same-day review)   :   yes",
        ],
    )
    def test_urgent_yes_variants(self, body):
        assert _urgent_state(body) == "on"

    @pytest.mark.parametrize(
        "body",
        [
            "- **Urgent** (needs same-day review): no",
            "- **Urgent**: no",
            "* **urgent**: NO",
        ],
    )
    def test_urgent_no_variants(self, body):
        assert _urgent_state(body) == "off"

    def test_complexity_yes(self):
        body = "- **High complexity** (non-obvious logic, careful review): yes"
        assert _complexity_state(body) == "on"

    def test_complexity_no(self):
        body = "- **High complexity** (non-obvious logic, careful review): no"
        assert _complexity_state(body) == "off"

    def test_value_must_be_yes_or_no(self):
        # "maybe" is not yes/no; field is treated as absent.
        body = "- **Urgent**: maybe"
        assert _urgent_state(body) is None

    def test_value_word_boundary(self):
        # "nothing" must not be parsed as "no".
        body = "- **Urgent**: nothing here"
        assert _urgent_state(body) is None

    def test_yes_or_no_inside_parenthetical_is_ignored(self):
        # The inline "yes or no" hint inside the parenthetical is descriptive;
        # only the value after the colon counts.
        body = "- **Urgent** (answer yes or no): no"
        assert _urgent_state(body) == "off"

    def test_absent_returns_none(self):
        assert _urgent_state("body with no template") is None


class TestFieldStateLegacyCheckbox:
    """Legacy template format: ``- [x] **Field**`` (in-flight PRs)."""

    def test_urgent_checked(self):
        body = "- [x] **Urgent**: needs same-day review"
        assert _urgent_state(body) == "on"

    def test_urgent_unchecked(self):
        body = "- [ ] **Urgent**: needs same-day review"
        assert _urgent_state(body) == "off"

    def test_urgent_capital_x(self):
        body = "- [X] **Urgent**"
        assert _urgent_state(body) == "on"

    def test_urgent_absent(self):
        body = "no template here"
        assert _urgent_state(body) is None

    def test_urgent_without_bold(self):
        body = "- [x] urgent: needs same-day review"
        assert _urgent_state(body) == "on"

    def test_complexity_checked(self):
        body = "- [x] **High complexity**: non-obvious logic"
        assert _complexity_state(body) == "on"

    def test_complexity_unchecked(self):
        body = "- [ ] **High complexity**: non-obvious logic"
        assert _complexity_state(body) == "off"

    def test_extra_whitespace(self):
        body = "-   [ x ]   **Urgent**: needs same-day review"
        assert _urgent_state(body) == "on"

    def test_asterisk_bullet(self):
        body = "* [x] urgent"
        assert _urgent_state(body) == "on"


class TestFieldStatePrecedence:
    """When both formats appear in the same body, yes/no wins."""

    def test_yesno_wins_over_legacy_checkbox(self):
        body = (
            "- [ ] **Urgent**: stale legacy line\n"
            "- **Urgent** (needs same-day review): yes"
        )
        assert _urgent_state(body) == "on"

    def test_yesno_no_wins_over_legacy_checked(self):
        body = (
            "- [x] **Urgent**: stale legacy line\n"
            "- **Urgent** (needs same-day review): no"
        )
        assert _urgent_state(body) == "off"

    def test_legacy_checked_with_yesno_in_description_is_on(self):
        # Regression: the yes/no regex must not treat the ``*`` from
        # ``**Urgent**`` inside a legacy checkbox line as a list bullet,
        # which would let it capture ``no`` from the description and
        # incorrectly flip a checked box from ``on`` to ``off``.
        body = "- [x] **Urgent**: no further action"
        assert _urgent_state(body) == "on"

    def test_legacy_checked_with_yes_in_description_is_on(self):
        body = "- [x] **Urgent**: yes please review today"
        assert _urgent_state(body) == "on"

    def test_legacy_unchecked_with_yes_in_description_is_off(self):
        body = "- [ ] **Urgent**: yes please review today"
        assert _urgent_state(body) == "off"

    def test_field_state_ignores_yesno_match_on_checkbox_line(self):
        # Defense in depth: even if the yes/no regex regresses to the old
        # unanchored form and matches inside a legacy checkbox line,
        # ``field_state`` must drop that match and use the checkbox.
        import re as _re

        unanchored = _re.compile(
            r"[-*]\s*[*_`]*\s*urgent\b[^:\n]*:\s*(yes|no)\b",
            _re.IGNORECASE,
        )
        body = "- [x] **Urgent**: no further action"
        # Sanity: the regressed regex really would mis-capture "no".
        assert unanchored.search(body).group(1) == "no"
        # field_state must still return "on" via the checkbox fallback.
        assert (
            pr_labeler.field_state(
                body,
                yesno=unanchored,
                checkbox=pr_labeler.URGENT_CHECKBOX_REGEX,
            )
            == "on"
        )

    def test_field_state_keeps_yesno_on_separate_line_from_checkbox(self):
        # The defensive filter must only ignore yes/no matches whose
        # enclosing line is itself a checkbox line. A real yes/no entry
        # on its own line still wins over an unrelated legacy line.
        body = (
            "- [x] **Urgent**: stale legacy line\n"
            "- **Urgent** (needs same-day review): no"
        )
        assert _urgent_state(body) == "off"


# ---- reconcile -------------------------------------------------------------


def _pr(*, additions=0, deletions=0, body="", labels=()):
    return {
        "additions": additions,
        "deletions": deletions,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "state": "OPEN",
    }


class TestReconcile:
    def test_adds_size_label_for_new_pr(self):
        plan = _plan()
        pr_labeler.reconcile(_pr(additions=5, deletions=2), plan=plan)
        assert "size/XS" in plan.add
        assert plan.remove == []

    def test_swaps_size_label_when_changed(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=300, deletions=0, labels=("size/S",)),
            plan=plan,
        )
        assert "size/L" in plan.add
        assert "size/S" in plan.remove

    def test_keeps_correct_size_label(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=300, deletions=0, labels=("size/L",)),
            plan=plan,
        )
        assert plan.add == []
        assert plan.remove == []

    def test_does_not_remove_manual_risk_when_no_bugbot(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5, body="no marker", labels=("risk/high",)),
            plan=plan,
        )
        assert "risk/high" not in plan.remove

    def test_swaps_risk_label_when_bugbot_changes(self):
        plan = _plan()
        body = "<!-- CURSOR_SUMMARY -->\n**Low Risk**"
        pr_labeler.reconcile(
            _pr(additions=5, body=body, labels=("risk/high",)),
            plan=plan,
        )
        assert "risk/low" in plan.add
        assert "risk/high" in plan.remove

    def test_urgent_yesno_yes_adds_label(self):
        plan = _plan()
        body = "- **Urgent** (needs same-day review): yes"
        pr_labeler.reconcile(_pr(additions=5, body=body), plan=plan)
        assert pr_labeler.URGENT_LABEL in plan.add

    def test_urgent_yesno_no_removes_label(self):
        plan = _plan()
        body = "- **Urgent** (needs same-day review): no"
        pr_labeler.reconcile(
            _pr(additions=5, body=body, labels=(pr_labeler.URGENT_LABEL,)),
            plan=plan,
        )
        assert pr_labeler.URGENT_LABEL in plan.remove

    def test_urgent_legacy_checked_adds_label(self):
        plan = _plan()
        body = "- [x] **Urgent**: needs same-day review"
        pr_labeler.reconcile(_pr(additions=5, body=body), plan=plan)
        assert pr_labeler.URGENT_LABEL in plan.add

    def test_urgent_legacy_unchecked_removes_label(self):
        plan = _plan()
        body = "- [ ] **Urgent**: needs same-day review"
        pr_labeler.reconcile(
            _pr(additions=5, body=body, labels=(pr_labeler.URGENT_LABEL,)),
            plan=plan,
        )
        assert pr_labeler.URGENT_LABEL in plan.remove

    def test_urgent_absent_leaves_manual_label(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5, body="no template", labels=(pr_labeler.URGENT_LABEL,)),
            plan=plan,
        )
        assert pr_labeler.URGENT_LABEL not in plan.remove

    def test_complexity_yesno_yes_adds_label(self):
        plan = _plan()
        body = "- **High complexity** (non-obvious logic, careful review): yes"
        pr_labeler.reconcile(_pr(additions=5, body=body), plan=plan)
        assert pr_labeler.COMPLEXITY_LABEL in plan.add


# ---- determine_targets ------------------------------------------------------


class TestDetermineTargets:
    def test_explicit_number(self, monkeypatch):
        targets = pr_labeler.determine_targets("repo", "42", "")
        assert targets == [42]

    def test_event_fallback(self, monkeypatch):
        targets = pr_labeler.determine_targets("repo", "", "99")
        assert targets == [99]

    def test_event_overridden_by_explicit(self, monkeypatch):
        targets = pr_labeler.determine_targets("repo", "10", "99")
        assert targets == [10]

    def test_no_input_returns_empty(self):
        assert pr_labeler.determine_targets("repo", "", "") == []


# ---- parse_codeowners -------------------------------------------------------


class TestParseCodeowners:
    def test_simple_catch_all(self):
        rules = pr_labeler.parse_codeowners("* @org/scanning")
        assert rules == [("*", ["scanning"])]

    def test_multiple_owners(self):
        rules = pr_labeler.parse_codeowners("proto/ @org/integrations @org/scanning")
        assert rules == [("proto/", ["integrations", "scanning"])]

    def test_skips_comments_and_blanks(self):
        text = (
            "# comment\n\n* @org/eng-leads\n  # indented comment\n/web/ @org/findings"
        )
        rules = pr_labeler.parse_codeowners(text)
        assert len(rules) == 2
        assert rules[0] == ("*", ["eng-leads"])
        assert rules[1] == ("/web/", ["findings"])

    def test_inline_comment_stripped(self):
        rules = pr_labeler.parse_codeowners("/vendor/ @org/platform # vendored deps")
        assert rules == [("/vendor/", ["platform"])]

    def test_owner_case_normalized(self):
        rules = pr_labeler.parse_codeowners("* @org/Integrations")
        assert rules[0][1] == ["integrations"]


# ---- _codeowners_match ------------------------------------------------------


class TestCodeownersMatch:
    def test_star_matches_everything(self):
        assert pr_labeler._codeowners_match("*", "any/file.py")
        assert pr_labeler._codeowners_match("*", "root.go")

    def test_anchored_dir(self):
        assert pr_labeler._codeowners_match("/web/", "web/app.py")
        assert pr_labeler._codeowners_match("/web/", "web/sub/deep.py")
        assert not pr_labeler._codeowners_match("/web/", "other/web/app.py")

    def test_unanchored_dir_with_internal_slash(self):
        # Pattern has internal slash -> implicitly anchored
        assert pr_labeler._codeowners_match("pkg/engine/", "pkg/engine/scan.go")
        assert not pr_labeler._codeowners_match("pkg/engine/", "other/pkg/engine/x.go")

    def test_anchored_glob(self):
        assert pr_labeler._codeowners_match(
            "/web/webapi/views/*.py", "web/webapi/views/foo.py"
        )
        assert not pr_labeler._codeowners_match(
            "/web/webapi/views/*.py", "web/webapi/views/sub/foo.py"
        )

    def test_unanchored_basename(self):
        assert pr_labeler._codeowners_match("go.sum", "go.sum")
        assert pr_labeler._codeowners_match("go.sum", "vendor/somelib/go.sum")

    def test_basename_glob(self):
        assert pr_labeler._codeowners_match("*.js", "frontend/app.js")
        assert pr_labeler._codeowners_match("*.js", "app.js")
        assert not pr_labeler._codeowners_match("*.js", "app.jsx")

    def test_deep_anchored_path(self):
        assert pr_labeler._codeowners_match(
            "/vendor/github.com/trufflesecurity/smallfetch/",
            "vendor/github.com/trufflesecurity/smallfetch/client.go",
        )
        assert not pr_labeler._codeowners_match(
            "/vendor/github.com/trufflesecurity/smallfetch/",
            "other/vendor/github.com/trufflesecurity/smallfetch/client.go",
        )

    def test_trailing_slash_no_leading_slash_matches_any_depth(self):
        # "vendor/" with no leading "/" should match at any depth
        assert pr_labeler._codeowners_match("vendor/", "vendor/file.go")
        assert pr_labeler._codeowners_match("vendor/", "nested/vendor/file.go")
        assert pr_labeler._codeowners_match("vendor/", "a/b/vendor/deep.go")

    def test_trailing_slash_with_leading_slash_anchored(self):
        assert pr_labeler._codeowners_match("/vendor/", "vendor/file.go")
        assert not pr_labeler._codeowners_match("/vendor/", "nested/vendor/file.go")


SAMPLE_CODEOWNERS = """\
* @org/eng-leads
/web/ @org/findings
/web/webapi/views/*.py @org/integrations
/pkg/engine/ @org/scanning
go.sum
go.mod
"""


# ---- reconcile with domain labels -------------------------------------------


class TestReconcileDomain:
    def test_adds_domain_labels(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5),
            plan=plan,
            domain_slugs={"scanning", "findings"},
        )
        assert "domain/scanning" in plan.add
        assert "domain/findings" in plan.add

    def test_removes_stale_domain_labels(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5, labels=("domain/scanning", "domain/platform")),
            plan=plan,
            domain_slugs={"scanning"},
        )
        assert "domain/scanning" not in plan.add  # already present
        assert "domain/scanning" not in plan.remove
        assert "domain/platform" in plan.remove

    def test_ignores_unknown_slugs(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5),
            plan=plan,
            domain_slugs={"eng-leads", "scanning"},
        )
        assert "domain/eng-leads" not in plan.add
        assert "domain/scanning" in plan.add

    def test_no_domain_changes_when_none(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5, labels=("domain/scanning",)),
            plan=plan,
            domain_slugs=None,
        )
        assert "domain/scanning" not in plan.remove


# ---- domain_reasons ---------------------------------------------------------


CO_OWNED_CODEOWNERS = """\
* @org/eng-leads
proto/ @org/integrations @org/scanning
"""

UNKNOWN_TEAM_CODEOWNERS = """\
* @org/eng-leads
/foo/ @org/mystery
"""


class TestDomainReasons:
    @pytest.fixture()
    def rules(self):
        return pr_labeler.parse_codeowners(SAMPLE_CODEOWNERS)

    def test_single_domain(self, rules):
        result = pr_labeler.domain_reasons(rules, ["pkg/engine/scan.go"])
        assert result == {"scanning": [("pkg/engine/scan.go", "/pkg/engine/")]}

    def test_multi_domain(self, rules):
        result = pr_labeler.domain_reasons(rules, ["web/app.py", "pkg/engine/scan.go"])
        assert result == {
            "findings": [("web/app.py", "/web/")],
            "scanning": [("pkg/engine/scan.go", "/pkg/engine/")],
        }

    def test_last_match_wins_pattern_reported(self, rules):
        # foo.py matches both /web/ and the views glob; the *reported* pattern
        # is the last match, not the directory default.
        result = pr_labeler.domain_reasons(rules, ["web/webapi/views/foo.py"])
        assert result == {
            "integrations": [("web/webapi/views/foo.py", "/web/webapi/views/*.py")]
        }

    def test_co_owned_file_under_both_slugs(self):
        rules = pr_labeler.parse_codeowners(CO_OWNED_CODEOWNERS)
        result = pr_labeler.domain_reasons(rules, ["proto/api.proto"])
        assert result == {
            "integrations": [("proto/api.proto", "proto/")],
            "scanning": [("proto/api.proto", "proto/")],
        }

    def test_unowned_file_no_entry(self, rules):
        # go.sum's last match has no owner -> no reason recorded.
        assert pr_labeler.domain_reasons(rules, ["go.sum"]) == {}

    def test_catch_all_recorded(self, rules):
        result = pr_labeler.domain_reasons(rules, ["README.md"])
        assert result == {"eng-leads": [("README.md", "*")]}

    def test_empty_files(self, rules):
        assert pr_labeler.domain_reasons(rules, []) == {}


# ---- render_domain_reasons --------------------------------------------------


class TestRenderDomainReasons:
    def test_empty_returns_empty_string(self):
        assert pr_labeler.render_domain_reasons({}) == ""

    def test_structure_markers_and_callout(self):
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/scan.go", "/pkg/engine/")]}
        )
        lines = out.splitlines()
        assert lines[0] == pr_labeler.DOMAIN_REASONS_START
        assert lines[1] == "---"
        assert lines[3] == "> [!IMPORTANT]"
        assert lines[-1] == pr_labeler.DOMAIN_REASONS_END
        assert lines[-2] == "</details>"
        # The visible callout is a blockquote; the collapsed breakdown lives in
        # a <details> whose inner list is plain (non-blockquoted) markdown so it
        # renders inside the block.
        assert "<details>" in lines
        assert "<summary>Which teams and files (1)</summary>" in lines
        assert "- **scanning** - scan engine, job control, reverification" in lines

    def test_known_slug_shows_charter_no_label(self):
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/scan.go", "/pkg/engine/")]}
        )
        assert "- **scanning** - scan engine, job control, reverification" in out
        assert "domain/scanning" not in out
        assert "`pkg/engine/scan.go` (matched `/pkg/engine/`)" in out

    def test_eng_leads_charter_present(self):
        out = pr_labeler.render_domain_reasons({"eng-leads": [("README.md", "*")]})
        assert (
            "- **eng-leads** - engineering leads; default reviewers for files "
            "no domain team owns" in out
        )

    def test_unknown_slug_bare_name_no_charter(self):
        # End-to-end: an owner with no DOMAIN_DESCRIPTIONS entry is still
        # attributed and rendered, just with a bare slug and no charter.
        rules = pr_labeler.parse_codeowners(UNKNOWN_TEAM_CODEOWNERS)
        reasons = pr_labeler.domain_reasons(rules, ["foo/x.go"])
        assert reasons == {"mystery": [("foo/x.go", "/foo/")]}
        out = pr_labeler.render_domain_reasons(reasons)
        assert "- **mystery**" in out
        assert "- **mystery** -" not in out  # no charter appended

    def test_deterministic_known_first_catch_all_last(self):
        reasons = {
            "eng-leads": [("README.md", "*")],
            "integrations": [("proto/x.proto", "proto/")],
            "scanning": [("pkg/engine/s.go", "/pkg/engine/")],
        }
        out = pr_labeler.render_domain_reasons(reasons)
        team_lines = [line for line in out.splitlines() if line.startswith("- **")]
        assert team_lines[0].startswith("- **scanning**")
        assert team_lines[1].startswith("- **integrations**")
        assert team_lines[2].startswith("- **eng-leads**")

    def test_file_cap_and_more(self):
        files = [(f"pkg/engine/f{i:02d}.go", "/pkg/engine/") for i in range(12)]
        out = pr_labeler.render_domain_reasons({"scanning": files})
        listed = [line for line in out.splitlines() if line.startswith("  - `")]
        assert len(listed) == pr_labeler.DOMAIN_REASON_FILE_CAP
        assert "  - ...and 2 more" in out

    def test_backtick_in_path_neutralized(self):
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/`evil`.go", "/pkg/engine/")]}
        )
        assert "`evil`" not in out
        assert "`pkg/evil.go`" in out

    def test_codeowners_link_when_url_provided(self):
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]},
            codeowners_url="https://github.com/org/repo/blob/HEAD/CODEOWNERS",
        )
        assert "[CODEOWNERS](https://github.com/org/repo/blob/HEAD/CODEOWNERS)" in out

    def test_codeowners_plain_when_no_url(self):
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        assert "per CODEOWNERS." in out
        assert "](http" not in out

    def test_leading_rule_present_by_default(self):
        # Body variant separates the block from the author's text with a rule.
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        lines = out.splitlines()
        assert lines[0] == pr_labeler.DOMAIN_REASONS_START
        assert lines[1] == "---"

    def test_leading_rule_omitted_for_comment(self):
        # Comment variant has nothing above it, so no rule.
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]},
            leading_rule=False,
        )
        lines = out.splitlines()
        assert lines[0] == pr_labeler.DOMAIN_REASONS_START
        assert "---" not in lines
        assert lines[1] == "> [!IMPORTANT]"

    def test_details_block_default_collapsed(self):
        # The breakdown is wrapped in a <details> with no `open` attribute, so
        # it stays collapsed until the reader clicks.
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        assert "<details>" in out
        assert "<details open" not in out
        assert "</details>" in out
        assert "<summary>Which teams and files (1)</summary>" in out

    def test_breakdown_inside_details_not_in_callout(self):
        # Team/file lines moved out of the callout blockquote into the details
        # block: the callout (everything before <details>) must not carry them.
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        callout = out.split("<details>", 1)[0]
        assert "scanning" not in callout
        assert "> - **scanning**" not in out
        assert "- **scanning**" in out

    def test_blank_line_after_summary(self):
        # A blank line must follow </summary> so GitHub renders the inner list.
        out = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        lines = out.splitlines()
        summary_idx = next(
            i for i, line in enumerate(lines) if line.startswith("<summary>")
        )
        assert lines[summary_idx + 1] == ""

    def test_team_count_and_verb_agreement(self):
        one = pr_labeler.render_domain_reasons(
            {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        )
        assert "1 team owns some of the files" in one
        assert "<summary>Which teams and files (1)</summary>" in one
        many = pr_labeler.render_domain_reasons(
            {
                "scanning": [("pkg/engine/s.go", "/pkg/engine/")],
                "eng-leads": [("README.md", "*")],
            }
        )
        assert "2 teams own some of the files" in many
        assert "<summary>Which teams and files (2)</summary>" in many


# ---- upsert_managed_section -------------------------------------------------

START = pr_labeler.DOMAIN_REASONS_START
END = pr_labeler.DOMAIN_REASONS_END


def _section(text: str = "hello") -> str:
    return f"{START}\n{text}\n{END}"


class TestUpsertManagedSection:
    def test_insert_when_absent_uses_blank_separator(self):
        body = "Author summary."
        section = _section()
        out = pr_labeler.upsert_managed_section(body, section, START, END)
        assert out == "Author summary.\n\n" + section

    def test_insert_into_empty_body(self):
        section = _section()
        assert pr_labeler.upsert_managed_section("", section, START, END) == section

    def test_replace_in_place_preserves_surroundings(self):
        body = f"Top\n\n{_section('old')}\n\nBottom"
        out = pr_labeler.upsert_managed_section(body, _section("new"), START, END)
        assert out == f"Top\n\n{_section('new')}\n\nBottom"

    def test_remove_restores_body_no_leftover_blanks(self):
        body = "Author summary."
        with_block = pr_labeler.upsert_managed_section(body, _section(), START, END)
        removed = pr_labeler.upsert_managed_section(with_block, "", START, END)
        assert removed == "Author summary."

    def test_remove_when_absent_is_noop(self):
        assert pr_labeler.upsert_managed_section("just text", "", START, END) == (
            "just text"
        )

    def test_malformed_single_marker_falls_back_to_append(self):
        # A lone START (END deleted by a user) must not corrupt the body via a
        # bad slice; treat as absent and append.
        body = f"Author text with a stray {START} marker."
        section = _section()
        out = pr_labeler.upsert_managed_section(body, section, START, END)
        assert out.endswith("\n\n" + section)

    def test_round_trip_idempotent(self):
        body = "Author summary."
        section = _section()
        once = pr_labeler.upsert_managed_section(body, section, START, END)
        twice = pr_labeler.upsert_managed_section(once, section, START, END)
        assert once == twice

    def test_render_then_upsert_idempotent(self):
        # End-to-end: rendering and re-upserting the same reasons is a no-op.
        reasons = {"scanning": [("pkg/engine/s.go", "/pkg/engine/")]}
        section = pr_labeler.render_domain_reasons(reasons)
        body = "Author summary."
        once = pr_labeler.upsert_managed_section(body, section, START, END)
        section2 = pr_labeler.render_domain_reasons(reasons)
        twice = pr_labeler.upsert_managed_section(once, section2, START, END)
        assert once == twice


# ---- sticky comment (REASONS_TARGET=comment) --------------------------------


class TestFindReasonsComment:
    def test_finds_comment_carrying_marker(self):
        comments = [
            {"id": 1, "body": "just a normal review comment"},
            {"id": 2, "body": f"{START}\nreasons\n{END}"},
        ]
        assert pr_labeler.find_reasons_comment(comments)["id"] == 2

    def test_returns_none_when_absent(self):
        comments = [{"id": 1, "body": "no marker here"}]
        assert pr_labeler.find_reasons_comment(comments) is None

    def test_ignores_comments_without_body(self):
        comments = [{"id": 1, "body": None}, {"id": 2, "body": f"{START}\nx\n{END}"}]
        assert pr_labeler.find_reasons_comment(comments)["id"] == 2

    def test_survivor_is_lowest_id_whatever_the_listing_order(self):
        # Two racing runs must agree on which copy is the original, or each
        # deletes the other's and the PR ends up with none.
        forward = [{"id": 9, "body": _section()}, {"id": 3, "body": _section()}]
        assert pr_labeler.find_reasons_comment(forward)["id"] == 3
        assert pr_labeler.find_reasons_comment(list(reversed(forward)))["id"] == 3


class TestFindReasonsComments:
    def test_returns_every_copy_lowest_id_first(self):
        comments = [
            {"id": 7, "body": _section("second copy")},
            {"id": 2, "body": "unrelated review comment"},
            {"id": 4, "body": _section("first copy")},
        ]
        assert [c["id"] for c in pr_labeler.find_reasons_comments(comments)] == [4, 7]

    def test_empty_when_no_copy_exists(self):
        assert pr_labeler.find_reasons_comments([{"id": 1, "body": "nope"}]) == []


class TestPruneDuplicateReasonsComments:
    def _stub_gh(self, monkeypatch):
        calls = []

        def fake_gh(args, check=True):
            calls.append({"args": args, "check": check})
            return pr_labeler.subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(pr_labeler, "gh", fake_gh)
        return calls

    def test_deletes_every_copy_but_the_lowest_id(self, monkeypatch):
        calls = self._stub_gh(monkeypatch)
        comments = [
            {"id": 3, "body": _section()},
            {"id": 8, "body": _section()},
            {"id": 11, "body": _section()},
        ]
        assert (
            pr_labeler.prune_duplicate_reasons_comments("org/repo", 1, comments, False)
            == 2
        )
        deleted = [c["args"][-1] for c in calls]
        assert deleted == [
            "repos/org/repo/issues/comments/8",
            "repos/org/repo/issues/comments/11",
        ]

    def test_single_copy_is_left_alone(self, monkeypatch):
        calls = self._stub_gh(monkeypatch)
        comments = [{"id": 3, "body": _section()}, {"id": 4, "body": "unrelated"}]
        assert (
            pr_labeler.prune_duplicate_reasons_comments("org/repo", 1, comments, False)
            == 0
        )
        assert calls == []

    def test_delete_tolerates_a_copy_another_run_already_removed(self, monkeypatch):
        calls = self._stub_gh(monkeypatch)
        comments = [{"id": 3, "body": _section()}, {"id": 8, "body": _section()}]
        pr_labeler.prune_duplicate_reasons_comments("org/repo", 1, comments, False)
        assert all(call["check"] is False for call in calls)

    def test_dry_run_counts_without_deleting(self, monkeypatch):
        calls = self._stub_gh(monkeypatch)
        comments = [{"id": 3, "body": _section()}, {"id": 8, "body": _section()}]
        assert (
            pr_labeler.prune_duplicate_reasons_comments("org/repo", 1, comments, True)
            == 1
        )
        assert calls == []


class TestPlanCommentAction:
    def test_create_when_none_and_section(self):
        action, note = pr_labeler.plan_comment_action(_section(), None)
        assert action == "create"
        assert "created" in note

    def test_noop_when_none_and_empty(self):
        assert pr_labeler.plan_comment_action("", None) is None

    def test_update_when_body_differs(self):
        existing = {"id": 9, "body": _section("old")}
        action, note = pr_labeler.plan_comment_action(_section("new"), existing)
        assert action == "update"
        assert "updated" in note

    def test_noop_when_body_matches(self):
        existing = {"id": 9, "body": _section("same")}
        assert pr_labeler.plan_comment_action(_section("same"), existing) is None

    def test_noop_when_body_matches_modulo_crlf_and_whitespace(self):
        # Round-tripped comment bodies may come back with CRLF / trailing space;
        # that must not trigger a perpetual "update".
        existing = {"id": 9, "body": _section("same").replace("\n", "\r\n") + "  \n"}
        assert pr_labeler.plan_comment_action(_section("same"), existing) is None

    def test_delete_when_empty_and_existing(self):
        existing = {"id": 9, "body": _section("stale")}
        action, note = pr_labeler.plan_comment_action("", existing)
        assert action == "delete"
        assert "removed" in note


# ---- supersede guard --------------------------------------------------------


class TestPrChangedFiles:
    """The 100-file window in `gh pr view --json files` must not reach CODEOWNERS.

    Truncation is silent, so these pin the tripwire in both directions: a short
    list must not provoke a second API call, and a capped one must be replaced
    wholesale rather than merged with the partial view.
    """

    def _stub_paged(self, monkeypatch, paths):
        calls = []

        def fake_fetch(repo, pr_number):
            calls.append((repo, pr_number))
            return paths

        monkeypatch.setattr(pr_labeler, "fetch_pr_file_paths", fake_fetch)
        return calls

    @staticmethod
    def _pr(count):
        return {"files": [{"path": f"vendor/a/f{i}.go"} for i in range(count)]}

    def test_short_list_is_trusted_without_a_second_call(self, monkeypatch):
        calls = self._stub_paged(monkeypatch, ["never/used.go"])
        files = pr_labeler.pr_changed_files("org/repo", 1, self._pr(6))
        assert len(files) == 6
        assert calls == []

    def test_one_under_the_cap_still_takes_the_fast_path(self, monkeypatch):
        calls = self._stub_paged(monkeypatch, ["never/used.go"])
        assert len(pr_labeler.pr_changed_files("org/repo", 1, self._pr(99))) == 99
        assert calls == []

    def test_capped_list_is_refetched_in_full(self, monkeypatch):
        # The regression: a Renovate bump whose only owned path sorts past the
        # window. The fast path would report zero owners for the whole PR.
        full = [f"vendor/a/f{i}.go" for i in range(100)] + ["src/owned.go"]
        calls = self._stub_paged(monkeypatch, full)
        files = pr_labeler.pr_changed_files("org/repo", 7041, self._pr(100))
        assert files == full
        assert calls == [("org/repo", 7041)]

    def test_missing_files_key_is_not_an_error(self, monkeypatch):
        calls = self._stub_paged(monkeypatch, ["never/used.go"])
        assert pr_labeler.pr_changed_files("org/repo", 1, {}) == []
        assert calls == []


class TestFetchPrFilePaths:
    def _stub_gh(self, monkeypatch, stdout):
        calls = []

        def fake_gh(args, check=True):
            calls.append(args)
            return pr_labeler.subprocess.CompletedProcess(args, 0, stdout, "")

        monkeypatch.setattr(pr_labeler, "gh", fake_gh)
        return calls

    def test_splits_owner_and_repo_into_query_variables(self, monkeypatch):
        calls = self._stub_gh(monkeypatch, "a.go\nb.go\n")
        assert pr_labeler.fetch_pr_file_paths("trufflesecurity/thog", 7041) == [
            "a.go",
            "b.go",
        ]
        args = calls[0]
        assert "--paginate" in args
        assert "owner=trufflesecurity" in args
        assert "name=thog" in args
        assert "number=7041" in args

    def test_pagination_contract_is_present_in_the_query(self):
        # gh drives --paginate off these two fields; losing either silently
        # returns only the first page and reintroduces the truncation bug.
        assert "$endCursor" in pr_labeler.PR_FILES_QUERY
        assert "hasNextPage" in pr_labeler.PR_FILES_QUERY

    def test_blank_lines_are_dropped(self, monkeypatch):
        self._stub_gh(monkeypatch, "a.go\n\nb.go\n\n")
        assert pr_labeler.fetch_pr_file_paths("org/repo", 1) == ["a.go", "b.go"]


class TestIsSuperseded:
    SNAPSHOT = {"body": "original body", "headRefOid": "abc123"}

    def _current(self, monkeypatch, body, sha):
        monkeypatch.setattr(
            pr_labeler,
            "fetch_pr_inputs",
            lambda repo, pr_number: {"body": body, "headRefOid": sha},
        )

    def test_unchanged_inputs_proceed(self, monkeypatch):
        self._current(monkeypatch, "original body", "abc123")
        assert pr_labeler.is_superseded("org/repo", 1, self.SNAPSHOT) is False

    def test_edited_body_stands_down(self, monkeypatch):
        # The checkbox case: a newer run already read the toggled body, so this
        # run must not apply removals it computed from the stale one.
        self._current(monkeypatch, "urgent now ticked", "abc123")
        assert pr_labeler.is_superseded("org/repo", 1, self.SNAPSHOT) is True

    def test_new_head_commit_stands_down(self, monkeypatch):
        self._current(monkeypatch, "original body", "def456")
        assert pr_labeler.is_superseded("org/repo", 1, self.SNAPSHOT) is True

    def test_absent_and_empty_body_are_the_same_input(self, monkeypatch):
        # `gh` returns null for an empty description; that must not read as a
        # change and strand every run.
        self._current(monkeypatch, None, "abc123")
        snapshot = {"body": "", "headRefOid": "abc123"}
        assert pr_labeler.is_superseded("org/repo", 1, snapshot) is False


# ---- _is_retryable_gh_error -------------------------------------------------


class TestIsRetryableGhError:
    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: something failed (HTTP 504)",
            "gh: We couldn't respond to your request in time.",
            "Client.Timeout exceeded while awaiting headers",
            "request timed out",
            "read tcp: connection reset by peer",
            "dial tcp: connection refused",
            "unexpected EOF",
            "error: RPC failed; curl 56 recv failure: early EOF",
            "You have exceeded a secondary rate limit",
        ],
    )
    def test_transient_signatures_are_retryable(self, stderr):
        assert pr_labeler._is_retryable_gh_error(stderr) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            "gh: Not Found (HTTP 404)",
            # A 404 whose message carries a number containing "504" must not be
            # mistaken for a transient 5xx: signatures anchor to "HTTP 50x".
            "gh: Not Found (HTTP 404) for pull request 5041",
            "could not resolve to a Repository with the name",
            # Primary rate limits reset minutes out and cannot recover within
            # the short retry budget, so they fail fast (only secondary do).
            "API rate limit exceeded for user ID 1",
            "",
        ],
    )
    def test_non_transient_signatures_are_not_retryable(self, stderr):
        assert pr_labeler._is_retryable_gh_error(stderr) is False


# ---- gh() retry -------------------------------------------------------------


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["gh", "x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestGhRetry:
    def test_retries_transient_then_succeeds(self, monkeypatch):
        results = [
            _completed(1, stderr="gh: HTTP 504"),
            _completed(1, stderr="request timed out"),
            _completed(0, stdout="ok"),
        ]
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            return results.pop(0)

        sleeps: list[float] = []
        monkeypatch.setattr(pr_labeler.subprocess, "run", fake_run)
        monkeypatch.setattr(pr_labeler.time, "sleep", lambda s: sleeps.append(s))

        result = pr_labeler.gh(["api", "x"])
        assert result.stdout == "ok"
        assert calls["n"] == 3
        assert len(sleeps) == 2

    def test_non_retryable_fails_fast(self, monkeypatch):
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            return _completed(1, stderr="gh: Not Found (HTTP 404)")

        sleeps: list[float] = []
        monkeypatch.setattr(pr_labeler.subprocess, "run", fake_run)
        monkeypatch.setattr(pr_labeler.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(subprocess.CalledProcessError):
            pr_labeler.gh(["api", "x"])
        assert calls["n"] == 1
        assert sleeps == []

    def test_non_retryable_returns_when_check_false(self, monkeypatch):
        monkeypatch.setattr(
            pr_labeler.subprocess,
            "run",
            lambda *a, **k: _completed(1, stderr="gh: Not Found (HTTP 404)"),
        )
        result = pr_labeler.gh(["api", "x"], check=False)
        assert result.returncode == 1

    def test_exhausts_retries_then_raises(self, monkeypatch):
        calls = {"n": 0}

        def fake_run(*args, **kwargs):
            calls["n"] += 1
            return _completed(1, stderr="gh: HTTP 504")

        sleeps: list[float] = []
        monkeypatch.setattr(pr_labeler.subprocess, "run", fake_run)
        monkeypatch.setattr(pr_labeler.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(subprocess.CalledProcessError):
            pr_labeler.gh(["api", "x"])
        assert calls["n"] == pr_labeler.GH_MAX_ATTEMPTS
        assert len(sleeps) == pr_labeler.GH_MAX_ATTEMPTS - 1


# ---- main() failure-rate floor ----------------------------------------------


def _ok_pr(number):
    return {
        "number": number,
        "state": "OPEN",
        "additions": 1,
        "deletions": 0,
        "body": "",
        "labels": [],
        "files": [],
    }


class TestMainFailureFloor:
    def _setup_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
        monkeypatch.setenv("PR_NUMBER", "all")
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("EVENT_PR_NUMBER", "")
        # body mode + no CODEOWNERS keeps every real gh() call out of the path.
        monkeypatch.setenv("REASONS_TARGET", "body")
        monkeypatch.setattr(pr_labeler, "fetch_codeowners", lambda repo: None)

    def test_single_failure_does_not_fail_run(self, monkeypatch):
        self._setup_env(monkeypatch)
        monkeypatch.setattr(pr_labeler, "determine_targets", lambda *a: [1])

        def boom(repo, pr_number):
            raise subprocess.CalledProcessError(1, ["gh"], "", "gh: HTTP 504")

        monkeypatch.setattr(pr_labeler, "fetch_pr", boom)
        assert pr_labeler.main() == 0

    def test_enough_failures_fail_run(self, monkeypatch):
        self._setup_env(monkeypatch)
        targets = list(range(1, 11))  # 10 PRs
        monkeypatch.setattr(pr_labeler, "determine_targets", lambda *a: targets)

        # Fail 3 of 10: 3 >= floor of 2 and 30% > 10%, so the run fails.
        def maybe_boom(repo, pr_number):
            if pr_number <= 3:
                raise subprocess.CalledProcessError(1, ["gh"], "", "gh: HTTP 504")
            return _ok_pr(pr_number)

        monkeypatch.setattr(pr_labeler, "fetch_pr", maybe_boom)
        assert pr_labeler.main() == 1
