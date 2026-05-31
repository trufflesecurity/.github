"""Tests for pr_labeler module.

Run with: python -m pytest .github/scripts/test_pr_labeler.py -v
"""

from __future__ import annotations

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
        rules = pr_labeler.parse_codeowners(
            "proto/ @org/integrations @org/scanning"
        )
        assert rules == [("proto/", ["integrations", "scanning"])]

    def test_skips_comments_and_blanks(self):
        text = "# comment\n\n* @org/eng-leads\n  # indented comment\n/web/ @org/findings"
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
        assert pr_labeler._codeowners_match("/web/webapi/views/*.py", "web/webapi/views/foo.py")
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


# ---- domains_for_pr ---------------------------------------------------------


SAMPLE_CODEOWNERS = """\
* @org/eng-leads
/web/ @org/findings
/web/webapi/views/*.py @org/integrations
/pkg/engine/ @org/scanning
go.sum
go.mod
"""


class TestDomainsForPr:
    @pytest.fixture()
    def rules(self):
        return pr_labeler.parse_codeowners(SAMPLE_CODEOWNERS)

    def test_single_domain(self, rules):
        result = pr_labeler.domains_for_pr(rules, ["pkg/engine/scan.go"])
        assert result == {"scanning"}

    def test_last_match_wins(self, rules):
        # web/webapi/views/foo.py matches both /web/ and /web/webapi/views/*.py;
        # last-match-wins means integrations, not findings.
        result = pr_labeler.domains_for_pr(rules, ["web/webapi/views/foo.py"])
        assert result == {"integrations"}

    def test_multi_domain_pr(self, rules):
        result = pr_labeler.domains_for_pr(
            rules, ["web/app.py", "pkg/engine/scan.go"]
        )
        assert result == {"findings", "scanning"}

    def test_catch_all_fallback(self, rules):
        result = pr_labeler.domains_for_pr(rules, ["README.md"])
        assert result == {"eng-leads"}

    def test_unowned_file(self, rules):
        # go.sum has no owners in CODEOWNERS -> empty slug list from last match
        result = pr_labeler.domains_for_pr(rules, ["go.sum"])
        assert result == set()

    def test_empty_files(self, rules):
        assert pr_labeler.domains_for_pr(rules, []) == set()


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
