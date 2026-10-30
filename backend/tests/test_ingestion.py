from backend.ingestion.extract import extract
from backend.ingestion.firewall import check
from backend.ingestion.types import Extraction


def test_extract_basic():
    ex = extract("Hey Sam — I don't know. so we wait... Really? Yes!")
    assert ex.sentence_lengths == [5, 3, 1, 1]
    assert ex.words["don't"] == 1 and ex.bigrams[("i", "don't")] == 1
    assert ex.openers == {"hey": 1, "so": 1, "really": 1, "yes": 1}
    assert ex.transitions.get("so") == 1
    assert ex.punctuation["—"] == 1 and ex.punctuation["…"] == 1
    assert ex.punctuation["?"] == 1 and ex.punctuation["!"] == 1 and ex.punctuation["."] == 1
    assert ex.lower_starts == 1 and ex.upper_starts == 3
    assert sum(ex.structures.values()) == 4
    assert all(sig in ex.structures for (_, sig) in ex.word_structures)
    assert extract("") == Extraction()


def test_extraction_from_dict_roundtrip():
    ex = extract("i'll send it later. thanks!")
    again = Extraction.from_any(ex.to_dict())
    assert again == ex
    assert Extraction.from_any({"bigrams": {"a b": 2}}).bigrams == {("a", "b"): 2}


def test_firewall(db):
    draft = db.insert_draft(persona="Default", intent="x", text="Sam, the meeting has moved to 3pm. Could you send the slides?")
    assert check("", "manual", {}, db) == (False, "empty text")
    ok, reason = check(draft["text"], "manual", {}, db)
    assert not ok and "draft" in reason
    ok, reason = check("Sam, the meeting has moved to 3pm. Could you send the slides? ", "manual", {}, db)
    assert not ok
    ok, reason = check("sam, the meeting has moved to 3pm. could you send the slides please", "manual", {}, db)
    assert not ok and "near-duplicate" in reason
    assert check("totally my own words here", "gmail", {"direction": "inbound"}, db)[0] is False
    assert check("totally my own words here", "wispr", {"generated": True}, db)[0] is False
    assert check("totally my own words here", "manual", {}, db) == (True, None)
