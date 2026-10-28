import duckdb


def test_pipeline_built(built):
    m = built["meta"]
    assert m["segments"] > 2500
    assert m["single_job_sample"] > 1000


def test_taxonomy_resolves_ambiguous_parts(built):
    con = duckdb.connect(str(built["settings"].db_path), read_only=True)
    tax = dict(con.execute("select part_number, role || ':' || component from part_taxonomy").fetchall())
    assert tax["TY25879B"] == "primary:BATTERY"        # 'WET CHAR' = wet charged battery
    assert tax["SE501406"] == "primary:STARTER"
    assert tax["SE502800"] == "primary:CONTROLLER"     # 'ENGINE C' resolved by 'PROGRAM NEW ECU' text
    assert tax["CRSE501406"].startswith("core")
    assert tax["AT365869"].startswith("consumable")
    # symptom-only segment ('NO START') is labelled by its parts
    jobs = con.execute("select job_key from segments where description = 'NO START'").fetchall()
    assert {j[0] for j in jobs} <= {"STARTER"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_battery_quote(client):
    r = client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "replace battery", "match": "job"})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["jobs"] == ["BATTERY"] and q["mode"] == "job_catalog"
    lab = q["labor"][0]
    assert 0.8 <= lab["p50_hours"] <= 3.5
    assert lab["p80_hours"] >= lab["p50_hours"]
    assert lab["level_used"] == "model"
    kit = q["parts"][0]["kit"]
    assert any(p["part_number"] == "TY25879B" for p in kit)
    assert q["similar_work_orders"]
    assert q["quote_id"]


def test_customer_supplied_parts(client):
    r = client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["BATTERY"], "include_parts": False})
    q = r.json()
    assert q["parts"] == [] and q["parts_included"] is False
    assert q["labor"][0]["p50_hours"] > 0


def test_combo_quote(client):
    r = client.post("/quotes", json={"make": "JD", "model": "544P", "job_text": "battery and starter R&R", "match": "job"})
    q = r.json()
    assert sorted(q["jobs"]) == ["BATTERY", "STARTER"]
    single = max(lab["p50_hours"] for lab in q["labor"])
    assert q["total"]["p50_hours"] >= single


def test_unknown_model_falls_back(client):
    r = client.post("/quotes", json={"make": "JD", "model": "333X", "job_codes": ["STARTER"]})
    q = r.json()
    assert r.status_code == 200
    assert q["machine"]["known_model"] is False and q["machine"]["base_model"] == "333"
    assert q["labor"][0]["level_used"] in {"base", "make_div", "division", "global"}


def test_symptom_gets_diagnosis(client):
    r = client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "machine not starting"})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "similar_history"
    fixes = {f["job_key"] for f in q["diagnosis"]["likely_fixes"]}
    assert {"BATTERY", "STARTER"} <= fixes
    r = client.get("/diagnose", params={"symptom": "no start", "make": "JD", "model": "333G"})
    assert r.json()["symptom"] == "NO_START"


def test_context_quote_matches_meaning(client):
    # not in the job list: answered from past segments with the same meaning, whatever the wording
    r = client.post("/quotes", json={"make": "JD", "model": "544P", "job_text": "valve lash adjustment"})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "similar_history"
    matched = {d["description"] for d in q["matched_descriptions"]}
    assert {"SET ENGINE VALVES", "ADJUST VALVE CLEARANCE", "AJUSTEMENT DES VALVES"} <= matched
    assert 2.5 <= q["total"]["p50_hours"] <= 9
    assert any(p["part_number"] == "R526607" for p in q["parts"][0]["kit"] + q["parts"][0]["optional"])
    assert all("BATTER" not in d for d in matched)
    # the job list alone does not know it
    assert client.post("/quotes", json={"make": "JD", "model": "544P", "job_text": "valve lash adjustment",
                                        "match": "job"}).status_code == 422
    # auto: context first, with the job-list answer attached for comparison
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "replace battery"}).json()
    assert q["mode"] == "similar_history" and q["job_list_check"]["jobs"] == ["BATTERY"]
    assert any(p["part_number"] == "TY25879B" for p in q["parts"][0]["kit"])
    # job list only: context check attached
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "replace battery", "match": "job"}).json()
    assert q["mode"] == "job_catalog" and q["context_check"]["matched_segments"] > 0
    assert client.get("/similar", params={"q": "set valves", "make": "JD", "model": "S780"}).status_code == 200


def test_bad_requests(client):
    assert client.post("/quotes", json={"make": "JD", "model": "333G"}).status_code == 422
    assert client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "zzqx wibble"}).status_code == 422
    assert client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["NOPE"]}).status_code == 422
    assert client.get("/quotes/doesnotexist").status_code == 404


def test_record_actual_and_accuracy(client):
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["PM_500"]}).json()
    assert client.post(f"/quotes/{q['quote_id']}/actual", json={"actual_hours": 4.5}).status_code == 200
    got = client.get(f"/quotes/{q['quote_id']}").json()
    assert got["actual"]["actual_hours"] == 4.5
    assert client.get("/accuracy").json()["with_actuals"] >= 1


def test_search_endpoints(client):
    jobs = client.get("/jobs", params={"q": "batt"}).json()
    assert jobs[0]["job_code"] == "BATTERY"
    models = client.get("/models", params={"make": "JD", "q": "333"}).json()
    assert {m["model"] for m in models} >= {"333G", "333P"}
    assert client.get("/").status_code == 200


def test_parts_only_quote(client):
    r = client.post("/quotes", json={"make": "JD", "model": "333G", "part_numbers": ["ty25879b"]})
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "parts_history"
    assert [j["job_code"] for j in q["inferred_jobs"]] == ["BATTERY"]
    assert 0.8 <= q["total"]["p50_hours"] <= 3.5
    assert any("BATTER" in d["description"] or "START" in d["description"] for d in q["matched_descriptions"])
    assert q["parts_query"][0]["part_number"] == "TY25879B"


def test_parts_for_two_repairs_are_combined(client):
    q = client.post("/quotes", json={"make": "JD", "model": "544P",
                                     "part_numbers": ["TY25879B", "SE501406", "CRSE501406"]}).json()
    assert q["total"]["method"] == "parts_history_combined"
    assert {j["job_code"] for j in q["inferred_jobs"]} == {"BATTERY", "STARTER"}
    assert q["total"]["p50_hours"] >= max(lab["p50_hours"] for lab in q["labor"])


def test_parts_edge_cases(client):
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "part_numbers": ["NOPE1", "TY25879B"]}).json()
    assert q["unknown_parts"] == ["NOPE1"] and any("NOPE1" in w for w in q["warnings"])
    assert client.post("/quotes", json={"make": "JD", "model": "333G", "part_numbers": ["NOPE1"]}).status_code == 422
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "part_numbers": ["51M7044"]}).json()
    assert any("consumables" in w for w in q["warnings"])
    # a description narrows a parts query
    q = client.post("/quotes", json={"make": "JD", "model": "544P", "part_numbers": ["R526607"],
                                     "job_text": "valve lash adjustment"}).json()
    assert q["mode"] == "parts_history" and q["total"]["p50_hours"] > 2
    lk = client.get("/parts/lookup", params={"part_numbers": "TY25879B, nope9"}).json()
    assert lk["known"][0]["component"] == "BATTERY" and lk["unknown"] == ["nope9"]


def test_parts_quote_suggests_description(client):
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "part_numbers": ["TY25879B"]}).json()
    top = q["suggested_descriptions"][0]["description"]
    assert "BATTER" in top and "START" not in top
    q = client.post("/quotes", json={"make": "JD", "model": "544P", "part_numbers": ["TY25879B", "SE501406"]}).json()
    combined = q["suggested_descriptions"][0]
    assert combined.get("combined") and "BATTER" in combined["description"] and "STARTER" in combined["description"]
    q = client.post("/quotes", json={"make": "JD", "model": "544P", "job_text": "valve lash adjustment"}).json()
    assert q["suggested_descriptions"][0]["segments"] > 10


def test_messy_make_model_are_standardised(client, built):
    con = duckdb.connect(str(built["settings"].db_path), read_only=True)
    makes = {r[0] for r in con.execute("select distinct make from segments").fetchall()}
    assert makes == {"JD"}                                    # JDE, DEERE, 'JD #2', 'John Deere' folded in
    models = {r[0] for r in con.execute("select distinct model from segments").fetchall()}
    assert "333G" in models and "333 G" not in models
    q = client.post("/quotes", json={"make": "John Deere", "model": "333 g", "job_codes": ["BATTERY"]}).json()
    assert q["machine"]["make"] == "JD" and q["machine"]["model_key"] == "333G"
    assert q["machine"]["make_entered"] == "John Deere"
    r = client.get("/machines/resolve", params={"make": "deere", "model": "872 gp"}).json()
    assert r["make"] == "JD" and r["model_key"] == "872GP"


def test_shop_and_offsite_hours(client):
    for body in ({"job_codes": ["BATTERY"]}, {"job_text": "valve lash adjustment"}, {"part_numbers": ["TY25879B"]}):
        q = client.post("/quotes", json={"make": "JD", "model": "333G", **body}).json()
        assert set(q["by_location"]) == {"SHOP", "OFFSITE"}, body
        assert q["by_location"]["SHOP"]["sample_size"] > 0
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["BATTERY"], "location": "Off site"}).json()
    assert q["total"]["location"] == "OFFSITE"
    assert q["total"]["p50_hours"] == q["by_location"]["OFFSITE"]["p50_hours"]
    assert client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["BATTERY"],
                                        "location": "moon"}).status_code == 422


def test_input_limits(client):
    assert client.post("/quotes", json={"make": "JD", "model": "333G", "job_text": "x" * 301}).status_code == 422
    assert client.post("/quotes", json={"make": "JD", "model": "333G",
                                        "part_numbers": [f"P{i}" for i in range(51)]}).status_code == 422


def test_ops_endpoints_and_request_id(client):
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert client.get("/version").status_code == 200
    r = client.get("/jobs", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"


def test_api_keys(built, monkeypatch):
    from fastapi.testclient import TestClient

    from quote_system.api.main import app

    monkeypatch.setenv("QS_API_KEYS", "k1,k2")
    monkeypatch.setenv("QS_ADMIN_KEY", "adm")
    try:
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200           # open
            assert c.get("/ready").status_code == 200            # open
            assert c.get("/jobs").status_code == 401
            assert c.get("/jobs", headers={"X-API-Key": "nope"}).status_code == 401
            assert c.get("/jobs", headers={"X-API-Key": "k2"}).status_code == 200
            assert c.post("/admin/reload", headers={"X-API-Key": "k1"}).status_code == 403
            assert c.post("/admin/reload", headers={"X-Admin-Key": "adm"}).status_code == 200
    finally:
        monkeypatch.delenv("QS_API_KEYS")
        monkeypatch.delenv("QS_ADMIN_KEY")
        with TestClient(app):
            pass  # restore open mode for the other tests


def test_bad_export_never_replaces_good_statistics(built, tmp_path):
    from dataclasses import replace

    import pytest

    from quote_system.pipeline import build_database
    from quote_system.validate import ValidationError

    s = replace(built["settings"], min_segments=10**9)
    target = tmp_path / "quote.duckdb"
    with pytest.raises(ValidationError, match="work-order segments"):
        build_database(s, target, skip_ingest=True)
    assert not target.exists()
    # a collapse versus the previous build is refused too
    s = replace(built["settings"], max_drop=0.0)
    import duckdb as _d

    con = _d.connect(str(built["settings"].db_path), read_only=True)
    prev = int(con.execute("select v from meta where k = 'segments'").fetchone()[0])
    con.close()
    from quote_system.validate import validate

    with pytest.raises(ValidationError, match="dropped"):
        validate(s.work_dir / "workorders.parquet", s.work_dir / "parts.parquet", s, prev * 2)
    report = (built["settings"].db_path.parent / "review" / "data_quality.md").read_text()
    assert "Make / model standardisation" in report


def test_similar_machines_learned_and_used(client, built):
    con = duckdb.connect(str(built["settings"].db_path), read_only=True)
    nb = {r[0] for r in con.execute("select neighbor from model_neighbors where make='JD' and model='333G'").fetchall()}
    assert "333P" in nb                                   # same platform: shares parts and work
    review = built["settings"].db_path.parent / "review" / "similar_machines.csv"
    assert review.exists()
    q = client.post("/quotes", json={"make": "JD", "model": "333G", "job_codes": ["BATTERY"]}).json()
    assert any(x["model"] == "333P" for x in q["machine"]["similar_machines"])
    assert "similar" in {b["level"] for b in q["labor"][0]["basis"]}
    # a model never seen borrows the similar machines of its series / base
    q = client.post("/quotes", json={"make": "JD", "model": "333X", "job_codes": ["STARTER"]}).json()
    assert q["machine"]["known_model"] is False and q["machine"]["similar_machines"]


def test_class_factor_is_bounded(built):
    from quote_system.engine import QuoteEngine

    e = QuoteEngine(built["settings"])
    m = e.machine("JD", "333G")
    cf = e.class_factor(m, "make_div", ["BATTERY"])
    assert cf is None or 0.3 <= cf["factor"] <= 3.0


def test_work_orders_shown_only_for_searched_model(client):
    from quote_system.normalize import model_key

    for body in ({"job_codes": ["BATTERY"]}, {"job_text": "valve lash adjustment"}, {"part_numbers": ["TY25879B", "SE501406"]}):
        q = client.post("/quotes", json={"make": "JD", "model": "333 G", **body}).json()
        assert q["similar_work_orders"], body
        assert {model_key(w["model"]) for w in q["similar_work_orders"]} == {"333G"}, body
    # a model with no history shows no other machines' work orders (hours still come from similar machines)
    q = client.post("/quotes", json={"make": "JD", "model": "333X", "job_codes": ["STARTER"]}).json()
    assert q["similar_work_orders"] == [] and q["total"]["p50_hours"] > 0


def test_messy_make_entries_resolve_with_warning(client):
    for entered, how in (("JHONDEERE", "fuzzy"), ("Jhon Deere", "word"), ("Deere & Co", "word"), ("JOHN DEER", "word")):
        r = client.get("/machines/resolve", params={"make": entered, "model": "333 G"})
        assert r.status_code == 200 and r.json()["make"] == "JD" and r.json()["make_resolved_by"] == how, r.text
    q = client.post("/quotes", json={"make": "Jhon Deere", "model": "333G", "job_codes": ["BATTERY"]}).json()
    assert q["machine"]["make"] == "JD" and q["labor"][0]["level_used"] == "model"
    assert any("closest known spelling" in w for w in q["warnings"])


def test_make_inferred_from_model(client):
    for entered in ("", "Komatsoo"):
        q = client.post("/quotes", json={"make": entered, "model": "333G", "job_codes": ["BATTERY"]}).json()
        assert q["machine"]["make"] == "JD" and q["machine"]["make_resolved_by"] == "from_model", q["machine"]
        assert q["labor"][0]["level_used"] == "model"
        assert any("only JD has model 333G" in w for w in q["warnings"])


def test_unknown_make_and_model_warns(client):
    q = client.post("/quotes", json={"make": "Komatsoo", "model": "PC138", "job_codes": ["BATTERY"]}).json()
    assert q["machine"]["make_resolved_by"] == "unknown"
    assert any("not in history" in w and "Komatsoo" in w for w in q["warnings"])


def test_placeholder_keys_refused():
    import pytest as _pytest

    from quote_system.api.main import check_keys
    with _pytest.raises(RuntimeError, match="placeholder"):
        check_keys(["change-me-long-random-key"], "")
    with _pytest.raises(RuntimeError, match="placeholder"):
        check_keys(["a" * 40], "change-me-admin-key")
    check_keys(["a" * 40], "b" * 40)   # real keys start fine


def test_make_entries_review_list(client):
    client.post("/quotes", json={"make": "JHONDEERE", "model": "333G", "job_codes": ["BATTERY"]})
    client.post("/quotes", json={"make": "Komatsoo", "model": "PC138", "job_codes": ["BATTERY"]})
    r = client.get("/admin/make-entries")
    assert r.status_code == 200, r.text
    got = {(e["entered"], e["read_as"], e["how"]) for e in r.json()["entries"]}
    assert ("JHONDEERE", "JD", "fuzzy") in got
    assert ("Komatsoo", "KOMATSOO", "unknown") in got
    assert "JHONDEERE,JD," in r.json()["make_aliases_csv"]


def test_backtest_metrics_all_zero_actuals():
    from quote_system.backtest import _metrics
    m = _metrics([(1.5, 0.0), (2.0, 0.0)])
    assert m["n"] == 2 and m["within_25pct"] is None and m["within_50pct"] is None


def test_failed_build_cleans_up_and_releases_file(built, tmp_path, monkeypatch):
    import pytest as _pytest

    from quote_system import pipeline
    out = tmp_path / "x.duckdb"
    monkeypatch.setattr(pipeline, "classify_parts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with _pytest.raises(RuntimeError, match="boom"):
        pipeline.build_database(built["settings"], out, skip_ingest=True)
    assert not out.exists()
    assert not out.with_suffix(".building.duckdb").exists()   # temp file removed, nothing locked
