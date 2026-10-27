from quote_system.config import REPO_ROOT
from quote_system.rules import jobs_from_text, normalize_text, symptoms_from_text

CFG = REPO_ROOT / "config"


def test_normalize_strips_accents_and_case():
    assert normalize_text("  Remplacer   la batterie élevée ") == "REMPLACER LA BATTERIE ELEVEE"


def test_component_jobs_from_text():
    assert jobs_from_text("replace battery", CFG) == ["BATTERY"]
    assert jobs_from_text("Battery R&R", CFG) == ["BATTERY"]
    assert jobs_from_text("REMPLACER BATTERIE", CFG) == ["BATTERY"]
    assert jobs_from_text("battery and starter", CFG) == ["BATTERY", "STARTER"]
    assert jobs_from_text("replace alternator", CFG) == ["ALTERNATOR"]


def test_exclusions_and_symptoms():
    assert "BATTERY" not in jobs_from_text("install battery disconnect", CFG)
    assert "UNDERCARRIAGE" not in jobs_from_text("program new controller", CFG)
    assert jobs_from_text("machine not starting", CFG) == []
    assert symptoms_from_text("machine not starting", CFG) == ["NO_START"]
    assert "HYD_LEAK" in symptoms_from_text("hydraulic leak on boom", CFG)


def test_pm_intervals_and_modifiers():
    assert jobs_from_text("500 hour service", CFG) == ["PM_500"]
    assert jobs_from_text("PERFORM 1000HR PM", CFG) == ["PM_1000"]
    # diagnosis is dropped when the repair itself is named
    assert jobs_from_text("diagnose and replace starter", CFG) == ["STARTER"]
    assert jobs_from_text("diagnose no start", CFG) == ["DIAGNOSE"]
