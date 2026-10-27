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
    single = max(l["p50_hours"] for l in q["labor"])
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
    assert q["total"]["p50_hours"] >= max(l["p50_hours"] for l in q["labor"])


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
