"""营养目标：蛋白区间夹紧、档案体重变化后的重算、过期提示。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.domain.planning import PROTEIN_MAX_G_PER_KG, PROTEIN_MIN_G_PER_KG, clamp_protein_band

PROFILE = {
    "goal": "fat_loss", "sex": "male", "birth_year": 1997, "height_cm": 178,
    "weight_kg": 88, "weekly_days": 3, "session_minutes": 40,
    "equipment": ["dumbbell"], "allergies": [], "diet_preferences": [],
}


def _plan(client: TestClient, headers: dict[str, str]) -> dict:
    return client.get("/api/v1/plans/current", headers=headers).json()["data"]


def _setup(client: TestClient, headers: dict[str, str]) -> dict:
    client.put("/api/v1/me/profile", headers=headers, json=PROFILE)
    draft = client.post("/api/v1/plans/drafts", headers=headers,
                        json={"instruction": "减脂，每周 3 天"}).json()["data"]
    client.post(f"/api/v1/plans/drafts/{draft['draft_id']}/confirm", headers=headers,
                json={"comment": "init"})
    return _plan(client, headers)


def test_clamp_caps_protein_at_two_g_per_kg() -> None:
    """skill 给 176g（88kg × 2.0）不变；换 65kg 后必须压到 130g 以内。"""
    heavy = clamp_protein_band(
        {"macros": {"protein_g": {"low_g": 132, "high_g": 176}}}, 88)
    assert heavy["macros"]["protein_g"]["high_g"] == 176

    light = clamp_protein_band(
        {"macros": {"protein_g": {"low_g": 132, "high_g": 176}}}, 65)
    assert light["macros"]["protein_g"]["high_g"] == PROTEIN_MAX_G_PER_KG * 65
    assert light["macros"]["protein_g"]["low_g"] >= PROTEIN_MIN_G_PER_KG * 65
    assert light["basis_weight_kg"] == 65


def test_plan_records_basis_weight(client: TestClient, auth_headers) -> None:
    plan = _setup(client, auth_headers)
    assert plan["nutrition"]["basis_weight_kg"] == 88
    protein = plan["nutrition"]["macros"]["protein_g"]
    assert protein["high_g"] <= PROTEIN_MAX_G_PER_KG * 88


def test_stale_targets_when_weight_drops(client: TestClient, auth_headers) -> None:
    _setup(client, auth_headers)
    client.put("/api/v1/me/profile", headers=auth_headers,
               json={**PROFILE, "weight_kg": 65})
    plan = _plan(client, auth_headers)
    nutrition = plan["nutrition"]
    assert nutrition["targets_stale"] is True
    assert "88" in nutrition["stale_hint"] and "65" in nutrition["stale_hint"]


def test_retarget_creates_new_version_with_new_targets(client: TestClient, auth_headers) -> None:
    before = _setup(client, auth_headers)
    client.put("/api/v1/me/profile", headers=auth_headers,
               json={**PROFILE, "weight_kg": 65})

    result = client.post(f"/api/v1/plans/{before['plan_id']}/retarget",
                         headers=auth_headers).json()
    assert result["code"] == 0
    data = result["data"]
    assert data["new_version"] == before["version"] + 1

    after = _plan(client, auth_headers)
    assert after["version"] == before["version"] + 1
    assert after["protein_target_g"] == round(1.6 * 65, 1)
    assert after["nutrition"]["basis_weight_kg"] == 65
    assert after["nutrition"]["targets_stale"] is False
    # 餐食也要跟着重建，否则"三餐合计"和目标对不上
    assert after["meals"], "重算后应该有餐食"
    total = sum(meal["kcal"] for meal in after["meals"] if meal["day_index"] == 1)
    assert abs(total - after["calorie_target"]) / after["calorie_target"] < 0.15


def test_retarget_keeps_history(client: TestClient, auth_headers) -> None:
    before = _setup(client, auth_headers)
    client.post(f"/api/v1/plans/{before['plan_id']}/retarget", headers=auth_headers)
    versions = client.get(f"/api/v1/plans/{before['plan_id']}/versions",
                          headers=auth_headers).json()["data"]
    rows = {row["version"]: row for row in versions["versions"]}
    assert before["version"] in rows and rows[before["version"]]["status"] == "superseded"
    assert versions["versions"][0]["status"] == "active"
