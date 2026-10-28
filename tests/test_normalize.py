from quote_system.config import REPO_ROOT
from quote_system.normalize import MakeResolver, base_of, build_make_map, clean_make, model_key, series_of

CFG = str(REPO_ROOT / "config")


def test_clean_make_and_model_key():
    assert clean_make("JD #2") == clean_make("JD#3") == clean_make("jd.") == "JD"
    assert clean_make("  John   Deere ") == "JOHN DEERE"
    assert model_key("470G LC") == model_key("470GLC") == model_key("470g-lc") == "470GLC"
    assert model_key("X9 1000") == "X91000"
    assert model_key("") == "UNKNOWN"


def test_series_and_base():
    assert series_of("135GLC") == series_of("135G") == "135G"
    assert series_of("948LII") == "948L"
    assert base_of(series_of("135GLC")) == "135"
    assert series_of("S780") == "S780" and series_of("X91000") == "X91000"


def test_make_map_merges_only_with_evidence():
    counts = [("JD", m, 1000) for m in ("135GLC", "333G", "544P", "770GP")]
    counts += [("CAT", m, 800) for m in ("320", "D6", "950M")]
    counts += [("JDE", "333G", 30), ("JDE", "544P", 10),   # spelling of JD, same models
               ("DEERE", "135GLC", 12),                      # alias file
               ("MH", "544P", 3), ("MH", "FORKLIFT1", 7),     # short code, weak evidence -> kept
               ("ZZTOP", "ZZ1", 40)]                         # unrelated brand -> kept
    d = build_make_map(counts, CFG)
    assert d["JDE"].canonical == "JD" and d["JDE"].method == "auto_prefix"
    assert d["DEERE"].canonical == "JD" and d["DEERE"].method == "alias"
    assert d["MH"].canonical == "MH"
    assert d["ZZTOP"].canonical == "ZZTOP"
    assert d["JD"].canonical == "JD" and d["CAT"].canonical == "CAT"


def test_request_time_make_resolution():
    r = MakeResolver({"JDE": "JD", "HITAC": "HIT"}, {"JD": 1000, "HIT": 300, "CAT": 200}, CFG)
    assert r.resolve("John Deere") == ("JD", "alias")
    assert r.resolve("jde")[0] == "JD"
    assert r.resolve("Hitachi") == ("HIT", "prefix")
    assert r.resolve("cat")[0] == "CAT"
    assert r.resolve("Nobody")[1] == "unknown"


def test_request_time_typos_and_extra_words():
    r = MakeResolver({"JDE": "JD", "HITAC": "HIT"}, {"JD": 1000, "HIT": 300, "CAT": 200}, CFG)
    assert r.resolve("Deere & Co") == ("JD", "word")          # a known alias word inside the entry
    assert r.resolve("JOHN DEER") == ("JD", "word")
    assert r.resolve("Jhon Deere") == ("JD", "word")          # DEERE is a known word
    assert r.resolve("JHONDEERE") == ("JD", "fuzzy")          # 2 letters swapped
    assert r.resolve("JDEERE") == ("JD", "fuzzy")
    assert r.resolve("")[1] == "empty"
    assert r.resolve("Komatsoo")[1] == "unknown"              # nothing close: never guessed
    assert r.resolve("CO")[1] == "unknown"                    # too short to guess


def test_fuzzy_tie_is_not_guessed():
    # 'ABCE' is one letter from both ABCD and ABCF: two different makes, so no guess
    r = MakeResolver({}, {"ABCD": 100, "ABCF": 100}, CFG)
    assert r.resolve("ABCE")[1] == "unknown"
