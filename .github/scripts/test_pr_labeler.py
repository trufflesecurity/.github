"""Tests for pr_labeler module.

Run with: python -m pytest .github/scripts/test_pr_labeler.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

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


# ---- checkbox_state --------------------------------------------------------


class TestCheckboxState:
    def test_urgent_checked(self):
        body = "- [x] **Urgent**: needs same-day review"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "on"

    def test_urgent_unchecked(self):
        body = "- [ ] **Urgent**: needs same-day review"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "off"

    def test_urgent_capital_x(self):
        body = "- [X] **Urgent**"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "on"

    def test_urgent_absent(self):
        body = "no template here"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) is None

    def test_urgent_without_bold(self):
        body = "- [x] urgent: needs same-day review"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "on"

    def test_complexity_checked(self):
        body = "- [x] **High complexity**: non-obvious logic"
        assert pr_labeler.checkbox_state(body, pr_labeler.COMPLEXITY_REGEX) == "on"

    def test_complexity_unchecked(self):
        body = "- [ ] **High complexity**: non-obvious logic"
        assert pr_labeler.checkbox_state(body, pr_labeler.COMPLEXITY_REGEX) == "off"

    def test_extra_whitespace(self):
        body = "-   [ x ]   **Urgent**: needs same-day review"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "on"

    def test_asterisk_bullet(self):
        body = "* [x] urgent"
        assert pr_labeler.checkbox_state(body, pr_labeler.URGENT_REGEX) == "on"


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

    def test_urgent_checkbox_on_adds_label(self):
        plan = _plan()
        body = "- [x] **Urgent**: needs same-day review"
        pr_labeler.reconcile(_pr(additions=5, body=body), plan=plan)
        assert pr_labeler.URGENT_LABEL in plan.add

    def test_urgent_checkbox_off_removes_label(self):
        plan = _plan()
        body = "- [ ] **Urgent**: needs same-day review"
        pr_labeler.reconcile(
            _pr(additions=5, body=body, labels=(pr_labeler.URGENT_LABEL,)),
            plan=plan,
        )
        assert pr_labeler.URGENT_LABEL in plan.remove

    def test_urgent_checkbox_absent_leaves_manual_label(self):
        plan = _plan()
        pr_labeler.reconcile(
            _pr(additions=5, body="no template", labels=(pr_labeler.URGENT_LABEL,)),
            plan=plan,
        )
        assert pr_labeler.URGENT_LABEL not in plan.remove


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
