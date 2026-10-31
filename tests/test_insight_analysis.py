from datetime import date, timedelta

from app.insight_analysis import evaluate_model, personal_analysis, predict_user


def _weekly_rows(user_count=12, week_count=5):
    rows = []
    start = date(2025, 1, 6)
    for user_index in range(user_count):
        user_id = f"user-{user_index + 1:04d}"
        user_rows = []
        for week_index in range(week_count):
            steps = 4000 + user_index * 300 + week_index * 250
            user_rows.append(
                {
                    "user_id": user_id,
                    "week_start": start + timedelta(days=week_index * 7),
                    "avg_resting_bpm": 82 - user_index * 0.7 - week_index * 0.3,
                    "avg_daily_steps": steps,
                    "avg_sleep_hours": 6 + user_index * 0.08 + week_index * 0.06,
                    "workout_minutes": 15 + user_index % 5 * 4 + week_index * 3,
                    "weight_change_kg_per_day": None,
                }
            )
        for index in range(1, week_count):
            previous = user_rows[index - 1]
            user_rows[index]["weight_change_kg_per_day"] = (
                0.00002 * (6500 - previous["avg_daily_steps"])
                + 0.0003 * (7 - previous["avg_sleep_hours"])
            )
        rows.extend(user_rows)
    return rows


def test_grouped_model_evaluates_and_predicts_without_training_on_selected_profile():
    rows = _weekly_rows()
    evaluation = evaluate_model(rows)
    prediction = predict_user(rows, "user-0001")

    assert evaluation["status"] == "ready"
    assert evaluation["profile_count"] == 12
    assert evaluation["sample_count"] == 48
    assert evaluation["mae_kg_per_day"] >= 0
    assert prediction["status"] == "ready"
    assert prediction["excluded_profile"] == "user-0001"
    assert prediction["training_profile_count"] == 11
    assert prediction["source_week"] == "2025-02-03"
    assert prediction["predicted_7_day_change_kg"] == (
        prediction["predicted_kg_per_day"] * 7
    )


def test_personal_analysis_reports_exploratory_associations():
    rows = _weekly_rows(user_count=1)
    analysis = personal_analysis(rows, "user-0001")

    assert analysis["paired_weeks"] == 4
    assert analysis["associations"][0]["correlation"] is not None
    assert analysis["associations"][0]["correlation"] < 0
    assert "exploratory" in analysis["guidance"]


def test_model_reports_insufficient_data_instead_of_fitting_too_little():
    rows = _weekly_rows(user_count=1, week_count=2)

    assert evaluate_model(rows)["status"] == "insufficient_data"
    assert predict_user(rows, "user-0001")["status"] == "insufficient_data"
