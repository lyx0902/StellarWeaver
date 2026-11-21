"""Fixed FakeModel trajectories with independent effect observations."""

from stellarweaver.evals.local_fixtures import report


async def test_fixed_fixture_reports_match_and_preserve_separate_denominators() -> None:
    first = await report()
    assert first == await report()
    assert first["security"] == {
        "planned": 3,
        "boundary_checks_passed": 3,
        "unauthorized_effects": 0,
    }
    assert first["utility"] == {"planned": 2, "passed": 2}
    assert first["tool_efficiency"] == {
        "planned": 5,
        "tool_calls": 10,
        "model_calls": 14,
        "approvals": 1,
    }
    rows = {case["instance_id"]: case for case in first["cases"]}
    assert rows["unapproved-search"]["oracle"]["mock_receiver_calls"] == 0
    assert rows["approved-search"]["oracle"]["mock_receiver_calls"] == 1
    assert rows["locate-edit-recover"]["repair_metrics"] == {
        "file_localization_hit_at_1": True,
        "failure_to_repair_recovered": True,
        "patch_matches_oracle": True,
    }
