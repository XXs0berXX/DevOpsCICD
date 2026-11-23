"""API tests. moto's mock_aws fakes AWS in-process, so no network or account is needed."""
import importlib
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "infra"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        import local_setup
        local_setup.main()
        import main
        importlib.reload(main)  # create boto3 clients inside the mock
        yield TestClient(main.app), main


def new_listing(c):
    return c.post("/api/listings", json={
        "title": "Test house", "city": "Lahore", "property_type": "house", "price": 100,
        "seller_name": "Sam", "seller_email": "Sam@Example.com"}).json()["id"]


def test_health(client):
    c, _ = client
    data = c.get("/api/health").json()
    assert data["status"] == "ok" and data["version"]


def test_listing_hides_seller_email(client):
    c, _ = client
    lid = new_listing(c)
    data = c.get(f"/api/listings/{lid}").json()
    assert data["title"] == "Test house" and "seller_email" not in data
    assert [l["id"] for l in c.get("/api/listings?city=lah").json()] == [lid]


def test_offer_publishes_event_and_only_seller_can_decide(client):
    c, main = client
    lid = new_listing(c)
    oid = c.post(f"/api/listings/{lid}/offers", json={
        "buyer_name": "Ali", "buyer_email": "ali@example.com", "amount": 90}).json()["id"]

    q = main.sqs.get_queue_url(QueueName=main.QUEUE_NAME)["QueueUrl"]
    event = json.loads(main.sqs.receive_message(QueueUrl=q)["Messages"][0]["Body"])
    assert event["type"] == "OfferMade" and event["offer"]["amount"] == 90

    assert c.post(f"/api/offers/{oid}/decision", json={"seller_email": "x@y.z", "decision": "accepted"}).status_code == 403
    assert c.post(f"/api/offers/{oid}/decision", json={"seller_email": "sam@example.com", "decision": "accepted"}).json()["status"] == "accepted"
    assert c.get(f"/api/listings/{lid}").json()["status"] == "under_offer"
