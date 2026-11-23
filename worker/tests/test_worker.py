import pytest

from worker import build_email

LISTING = {"id": "l1", "title": "Test house", "city": "Lahore", "seller_name": "Sam", "seller_email": "sam@example.com"}
OFFER = {"id": "o1", "buyer_name": "Ali", "buyer_email": "ali@example.com", "amount": 90000, "message": "Hi"}


def test_offer_made_goes_to_seller():
    to, subject, body = build_email({"type": "OfferMade", "offer": OFFER, "listing": LISTING}, "http://site")
    assert to == "sam@example.com"
    assert "90,000" in body and "http://site/dashboard.html?email=sam@example.com" in body


@pytest.mark.parametrize("kind,word", [("OfferAccepted", "accepted"), ("OfferRejected", "rejected")])
def test_decision_goes_to_buyer(kind, word):
    to, subject, _ = build_email({"type": kind, "offer": OFFER, "listing": LISTING}, "http://site")
    assert to == "ali@example.com" and word in subject


def test_unknown_event_raises():
    with pytest.raises(ValueError):
        build_email({"type": "Nope", "offer": OFFER, "listing": LISTING})
