"""计划生成领域测试：目标计算口径、安全门拒绝、过敏原过滤、版本递增。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.planning import (
    _meal_for_slot,
    calculate_targets,
    validate_targets,
)
from app.main import create_app
from app.models import Profile


def _login(client: TestClient) -> str:
    """demo 登录并取 token。"""
    response = client.post("/api/v1/auth/demo", json={})
    return response.json()["data"]["access_token"]


def test_targets_are_in_safe_range() -> None:
    """标准画像算出的目标必须落在安全区间，且能通过目标安全门。"""
    profile = Profile(
        user_id="u1",
        sex="male",
        birth_year=1997,
        height_cm=178,
        weight_kg=78,
        activity_level="moderate",
        weekly_days=3,
    )
    targets = calculate_targets(profile)
    assert targets.calories >= 1500
    assert targets.calories < targets.tdee
    assert 1.2 * 78 <= targets.protein_g <= 2.2 * 78
    assert validate_targets(profile, targets) == []


def test_aggressive_small_profile_is_rejected() -> None:
    """体重低、活动量小的画像会触发热量下限/缺口规则，必须被拒绝。"""
    profile = Profile(
        user_id="u2",
        sex="female",
        birth_year=2010,
        height_cm=150,
        weight_kg=45,
        activity_level="sedentary",
        weekly_days=2,
    )
    targets = calculate_targets(profile)
    errors = validate_targets(profile, targets)
    assert errors


def test_allergen_filter() -> None:
    """选餐时必须排除含用户过敏原的餐食。"""
    profile = Profile(user_id="u3", allergies=["fish"])
    meal = _meal_for_slot(profile, "meal_dinner")
    assert "fish" not in meal["allergens"]


def test_generate_plan_creates_next_version(tmp_path) -> None:
    """重新生成应把 v1 顶成 v2，并让 /plans/current 指向新版本。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        secret_key="test-secret-0123456789abcdef0123456789",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/api/v1/plans/generate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        assert plan["version"] == 2
        assert len(plan["actions"]) == 12
        assert plan["calorie_target"] >= 1500

        current = client.get(
            "/api/v1/plans/current",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["data"]
        assert current["version"] == 2
