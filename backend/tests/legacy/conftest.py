"""Retired V1 contract checks. No production semantic capability claim is made here."""
import pytest
from app.agent import coach_agent, legacy_coach_agent

@pytest.fixture(autouse=True)
def retired_v1_runtime(monkeypatch):
    monkeypatch.setattr(coach_agent, "run", legacy_coach_agent.run)
