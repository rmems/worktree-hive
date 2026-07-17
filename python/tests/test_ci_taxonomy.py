"""Tests for the worktrees_hives CI taxonomy module."""

from __future__ import annotations

import pytest

from worktrees_hives.ci_taxonomy import (
    CheckClass,
    CheckConclusion,
    Policy,
    classify_check,
    classify_checks,
    parse_check_entry,
    rerun_command,
    should_rerun,
)


def _make_raw_check(
    *,
    name: str = "CI / test",
    workflow_name: str = "CI",
    conclusion: str = "failure",
    status: str = "completed",
    description: str = "",
    details_url: str = "https://github.com/owner/repo/actions/runs/12345",
) -> dict:
    return {
        "name": name,
        "workflowName": workflow_name,
        "conclusion": conclusion,
        "status": status,
        "description": description,
        "detailsUrl": details_url,
    }


class TestParseCheckEntry:
    def test_basic_parse(self):
        raw = _make_raw_check()
        entry = parse_check_entry(raw)
        assert entry.name == "CI / test"
        assert entry.workflow_name == "CI"
        assert entry.conclusion == CheckConclusion.FAILURE
        assert entry.status == "completed"
        assert entry.run_id == 12345

    def test_missing_fields_default_gracefully(self):
        entry = parse_check_entry({})
        assert entry.name == ""
        assert entry.conclusion == CheckConclusion.PENDING
        assert entry.run_id is None

    def test_none_fields_default_gracefully(self):
        raw = {
            "name": None,
            "workflowName": None,
            "conclusion": None,
            "status": None,
            "description": None,
            "detailsUrl": None,
        }
        entry = parse_check_entry(raw)
        assert entry.name == ""
        assert entry.conclusion == CheckConclusion.PENDING

    def test_unknown_conclusion_defaults_to_pending(self):
        raw = _make_raw_check(conclusion="weird_value")
        entry = parse_check_entry(raw)
        assert entry.conclusion == CheckConclusion.PENDING

    def test_run_id_extracted_from_url(self):
        raw = _make_raw_check(details_url="https://github.com/o/r/actions/runs/99999")
        entry = parse_check_entry(raw)
        assert entry.run_id == 99999

    def test_run_id_none_when_no_url(self):
        raw = _make_raw_check(details_url="")
        entry = parse_check_entry(raw)
        assert entry.run_id is None

    def test_state_field_parsed(self):
        raw = {"name": "CI", "state": "failure", "status": "completed"}
        entry = parse_check_entry(raw)
        assert entry.conclusion == CheckConclusion.FAILURE

    def test_workflow_field_parsed(self):
        raw = {"name": "test", "workflow": "CI"}
        entry = parse_check_entry(raw)
        assert entry.workflow_name == "CI"

    def test_link_field_parsed(self):
        raw = {"name": "test", "link": "https://github.com/o/r/actions/runs/42"}
        entry = parse_check_entry(raw)
        assert entry.run_id == 42

    def test_legacy_fields_still_work(self):
        raw = {
            "name": "test",
            "workflowName": "CI",
            "conclusion": "success",
            "detailsUrl": "https://github.com/o/r/actions/runs/1",
        }
        entry = parse_check_entry(raw)
        assert entry.workflow_name == "CI"
        assert entry.conclusion == CheckConclusion.SUCCESS
        assert entry.run_id == 1


class TestClassifyClassA:
    @pytest.mark.parametrize(
        "name,workflow_name",
        [
            ("CI / test (pull_request)", "CI"),
            ("Build / compile (pull_request)", "Build"),
            ("Lint / ruff (pull_request)", "Lint"),
            ("Rust / check (pull_request)", "Rust"),
            ("Python / test (pull_request)", "Python"),
            ("Tests / unit (pull_request)", "Tests"),
            ("Quality / lint (pull_request)", "Quality"),
        ],
    )
    def test_class_a_patterns(self, name, workflow_name):
        entry = parse_check_entry(_make_raw_check(name=name, workflow_name=workflow_name))
        classified = classify_check(entry)
        assert classified.check_class == CheckClass.A

    def test_class_a_has_fix_and_rerun_policies(self):
        entry = parse_check_entry(_make_raw_check())
        classified = classify_check(entry)
        assert Policy.FIX_SOURCE in classified.policies
        assert Policy.RERUN in classified.policies
        assert Policy.REPLY_WITH_SHA in classified.policies
        assert Policy.FORBID_EMPTY_COMMIT in classified.policies
        assert Policy.FORBID_IGNORE in classified.policies

    def test_unknown_check_with_actions_url_is_class_a(self):
        """Owned Actions runs without name heuristics are still Class A."""
        entry = parse_check_entry(
            _make_raw_check(
                name="cargo audit",
                workflow_name="Security",
                details_url="https://github.com/o/r/actions/runs/42",
            )
        )
        classified = classify_check(entry)
        assert classified.check_class == CheckClass.A
        assert Policy.FIX_SOURCE in classified.policies

    def test_unknown_check_without_actions_url_defaults_to_class_c(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="unknown-bot",
                workflow_name="unknown",
                details_url="https://circleci.com/gh/o/r/1",
            )
        )
        classified = classify_check(entry)
        assert classified.check_class == CheckClass.C


class TestClassifyClassB:
    @pytest.mark.parametrize(
        "name,workflow_name,description",
        [
            ("Codacy Analysis", "Codacy", ""),
            ("Code Climate", "Code Climate", ""),
            ("SonarCloud", "SonarCloud", ""),
            ("SonarQube", "SonarQube", ""),
            ("DeepSource", "DeepSource", ""),
            ("Code Analysis", "", "Static code analysis"),
        ],
    )
    def test_class_b_patterns(self, name, workflow_name, description):
        entry = parse_check_entry(
            _make_raw_check(name=name, workflow_name=workflow_name, description=description)
        )
        classified = classify_check(entry)
        assert classified.check_class == CheckClass.B

    def test_class_b_has_fix_but_no_rerun(self):
        entry = parse_check_entry(_make_raw_check(name="Codacy Analysis", workflow_name="Codacy"))
        classified = classify_check(entry)
        assert Policy.FIX_SOURCE in classified.policies
        assert Policy.RERUN not in classified.policies


class TestClassifyClassC:
    @pytest.mark.parametrize(
        "name,workflow_name",
        [
            ("Dependabot", "Dependabot"),
            ("Renovate", "Renovate"),
            ("Codecov", "Codecov"),
            ("Coveralls", "Coveralls"),
            ("Snyk Security", "Snyk"),
            ("Copilot Review", "Copilot"),
            ("Code Review", "Code Review"),
        ],
    )
    def test_class_c_patterns(self, name, workflow_name):
        entry = parse_check_entry(_make_raw_check(name=name, workflow_name=workflow_name))
        classified = classify_check(entry)
        assert classified.check_class == CheckClass.C

    def test_class_c_has_report_only_policies(self):
        entry = parse_check_entry(_make_raw_check(name="Dependabot", workflow_name="Dependabot"))
        classified = classify_check(entry)
        assert Policy.REPORT_ONLY in classified.policies
        assert Policy.MARK_RESIDUAL in classified.policies
        assert Policy.RERUN in classified.policies  # has run_id from default URL
        assert Policy.FIX_SOURCE not in classified.policies

    def test_class_c_without_run_id_has_no_rerun(self):
        entry = parse_check_entry(
            _make_raw_check(name="Dependabot", workflow_name="Dependabot", details_url="")
        )
        classified = classify_check(entry)
        assert Policy.RERUN not in classified.policies


class TestConclusionHandling:
    def test_success_has_no_policies(self):
        entry = parse_check_entry(_make_raw_check(conclusion="success"))
        assert classify_check(entry).policies == frozenset()

    def test_failure_has_policies(self):
        entry = parse_check_entry(_make_raw_check(conclusion="failure"))
        assert len(classify_check(entry).policies) > 0

    def test_cancelled_has_policies(self):
        entry = parse_check_entry(_make_raw_check(conclusion="cancelled"))
        assert len(classify_check(entry).policies) > 0


class TestClassifyChecks:
    def test_empty_list(self):
        # Empty checks are unknown — not success-by-empty.
        report = classify_checks([])
        assert report.all_passed is False

    def test_all_passed(self):
        raw = [_make_raw_check(conclusion="success")]
        assert classify_checks(raw).all_passed

    def test_pending_has_no_action_policies(self):
        raw = [_make_raw_check(name="CI", conclusion="pending")]
        classified = classify_checks(raw).checks[0]
        assert classified.policies == frozenset()
        assert should_rerun(classified) is False
        assert rerun_command(classified) is None

    def test_pending_not_all_passed(self):
        raw = [_make_raw_check(conclusion="pending", status="queued")]
        assert not classify_checks(raw).all_passed

    def test_cancelled_counted_as_failure(self):
        raw = [_make_raw_check(conclusion="cancelled")]
        report = classify_checks(raw)
        assert len(report.failures) == 1
        assert not report.all_passed

    def test_mixed_results(self):
        raw = [
            _make_raw_check(name="CI", conclusion="success"),
            _make_raw_check(name="Codacy", workflow_name="Codacy", conclusion="failure"),
            _make_raw_check(name="Dependabot", workflow_name="Dependabot", conclusion="failure"),
        ]
        report = classify_checks(raw)
        assert not report.all_passed
        assert len(report.failures) == 2

    def test_fixable_vs_residual(self):
        raw = [
            _make_raw_check(name="CI", conclusion="failure"),
            _make_raw_check(name="Codacy", workflow_name="Codacy", conclusion="failure"),
            _make_raw_check(name="Dependabot", workflow_name="Dependabot", conclusion="failure"),
        ]
        report = classify_checks(raw)
        assert len(report.fixable_failures) == 2
        assert len(report.residual_failures) == 1


class TestShouldRerun:
    def test_class_a_failure_should_not_rerun(self):
        entry = parse_check_entry(_make_raw_check(conclusion="failure"))
        assert should_rerun(classify_check(entry)) is False

    def test_class_a_timeout_should_rerun(self):
        entry = parse_check_entry(_make_raw_check(conclusion="timed_out"))
        assert should_rerun(classify_check(entry)) is True

    def test_class_b_should_not_rerun(self):
        entry = parse_check_entry(
            _make_raw_check(name="Codacy", workflow_name="Codacy", conclusion="failure")
        )
        assert should_rerun(classify_check(entry)) is False

    def test_class_c_with_run_id_should_rerun_on_timeout(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="Dependabot",
                workflow_name="Dependabot",
                conclusion="timed_out",
                details_url="https://github.com/o/r/actions/runs/123",
            )
        )
        assert should_rerun(classify_check(entry)) is True

    def test_class_c_without_run_id_should_not_rerun(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="Dependabot",
                workflow_name="Dependabot",
                conclusion="failure",
                details_url="",
            )
        )
        assert should_rerun(classify_check(entry)) is False

    def test_class_c_transient_without_run_id_should_not_rerun(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="Dependabot",
                workflow_name="Dependabot",
                conclusion="timed_out",
                details_url="",
            )
        )
        assert should_rerun(classify_check(entry)) is False


class TestRerunCommand:
    def test_class_a_timeout_returns_command(self):
        entry = parse_check_entry(
            _make_raw_check(
                conclusion="timed_out",
                details_url="https://github.com/o/r/actions/runs/999",
            )
        )
        assert rerun_command(classify_check(entry)) == ["ci", "rerun", "999"]

    def test_class_a_failure_returns_none(self):
        entry = parse_check_entry(
            _make_raw_check(
                conclusion="failure",
                details_url="https://github.com/o/r/actions/runs/999",
            )
        )
        assert rerun_command(classify_check(entry)) is None

    def test_class_b_returns_none(self):
        entry = parse_check_entry(
            _make_raw_check(name="Codacy", workflow_name="Codacy", conclusion="failure")
        )
        assert rerun_command(classify_check(entry)) is None


class TestClassificationPriority:
    def test_codacy_overrides_actions(self):
        entry = parse_check_entry(
            _make_raw_check(name="Codacy CI Analysis", workflow_name="Codacy CI")
        )
        assert classify_check(entry).check_class == CheckClass.B

    def test_third_party_overrides_actions(self):
        entry = parse_check_entry(
            _make_raw_check(name="Copilot CI Review", workflow_name="Copilot")
        )
        assert classify_check(entry).check_class == CheckClass.C


class TestGhBucketAndStateAliases:
    def test_gh_bucket_pass_maps_to_success(self):
        entry = parse_check_entry({"name": "CI", "bucket": "pass"})
        assert entry.conclusion == CheckConclusion.SUCCESS

    def test_gh_bucket_fail_maps_to_failure(self):
        entry = parse_check_entry({"name": "CI", "bucket": "fail"})
        assert entry.conclusion == CheckConclusion.FAILURE

    def test_gh_bucket_skipping_maps_to_skipped(self):
        entry = parse_check_entry({"name": "CI", "bucket": "skipping"})
        assert entry.conclusion == CheckConclusion.SKIPPED

    def test_gh_bucket_cancel_maps_to_cancelled(self):
        entry = parse_check_entry({"name": "CI", "bucket": "cancel"})
        assert entry.conclusion == CheckConclusion.CANCELLED

    def test_error_state_maps_to_failure(self):
        entry = parse_check_entry({"name": "CI", "state": "ERROR"})
        assert entry.conclusion == CheckConclusion.FAILURE

    def test_bucket_only_pass_is_all_passed(self):
        report = classify_checks(
            [
                {
                    "name": "CI / test",
                    "workflow": "CI",
                    "bucket": "pass",
                    "link": "https://github.com/o/r/actions/runs/1",
                }
            ]
        )
        assert report.all_passed
        assert report.failures == []

    def test_bucket_only_fail_is_failure(self):
        report = classify_checks(
            [
                {
                    "name": "CI / test",
                    "workflow": "CI",
                    "bucket": "fail",
                    "link": "https://github.com/o/r/actions/runs/1",
                }
            ]
        )
        assert not report.all_passed
        assert len(report.failures) == 1
        assert report.failures[0].entry.conclusion == CheckConclusion.FAILURE

    def test_error_state_is_failure(self):
        report = classify_checks(
            [
                {
                    "name": "CI / test",
                    "workflow": "CI",
                    "state": "ERROR",
                    "link": "https://github.com/o/r/actions/runs/1",
                }
            ]
        )
        assert len(report.failures) == 1


class TestClassARequiresActionsLink:
    def test_external_ci_name_without_actions_link_is_class_c(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="ci/circleci",
                workflow_name="",
                details_url="https://circleci.com/gh/o/r/123",
            )
        )
        assert classify_check(entry).check_class == CheckClass.C
        assert Policy.FIX_SOURCE not in classify_check(entry).policies
        assert Policy.REPORT_ONLY in classify_check(entry).policies

    def test_buildkite_without_actions_link_is_class_c(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="buildkite/build",
                workflow_name="",
                details_url="https://buildkite.com/o/r/builds/1",
            )
        )
        assert classify_check(entry).check_class == CheckClass.C

    def test_unit_tests_with_actions_link_is_class_a(self):
        entry = parse_check_entry(
            _make_raw_check(
                name="unit tests",
                workflow_name="CI",
                details_url="https://github.com/o/r/actions/runs/55",
            )
        )
        assert classify_check(entry).check_class == CheckClass.A


class TestStaleIsBlocker:
    def test_stale_counted_as_failure(self):
        raw = [_make_raw_check(conclusion="stale")]
        report = classify_checks(raw)
        assert len(report.failures) == 1
        assert not report.all_passed
        assert report.failures[0].entry.conclusion == CheckConclusion.STALE
