"""NestMarket API - listings, search, photo upload URLs and offers.

Runs unchanged against real AWS or a local emulator: boto3 reads AWS_ENDPOINT_URL,
so locally every call goes to moto instead of AWS.
"""
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Literal

import boto3
from boto3.dynamodb.conditions import Attr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

REGION = os.getenv("AWS_REGION", "us-east-1")
LISTINGS_TABLE = os.getenv("LISTINGS_TABLE", "nestmarket-listings")
OFFERS_TABLE = os.getenv("OFFERS_TABLE", "nestmarket-offers")
PHOTOS_BUCKET = os.getenv("PHOTOS_BUCKET", "nestmarket-photos")
QUEUE_NAME = os.getenv("QUEUE_NAME", "nestmarket-offer-events")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
# The CI/CD build writes the version (e.g. 1.0.7) into api/VERSION; locally it is "dev".
_VERSION_FILE = Path(__file__).with_name("VERSION")
APP_VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"

dynamodb = boto3.resource("dynamodb", region_name=REGION)
listings = dynamodb.Table(LISTINGS_TABLE)
offers = dynamodb.Table(OFFERS_TABLE)
s3 = boto3.client("s3", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)

app = FastAPI(title="NestMarket API")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# ---------- request bodies ----------
class UploadRequest(BaseModel):
    filename: str
    content_type: str = "image/jpeg"


class ListingIn(BaseModel):
    title: str
    city: str
    property_type: Literal["house", "flat", "plot"]
    price: int = Field(gt=0)
    bedrooms: int = 0
    description: str = ""
    seller_name: str
    seller_email: str
    photo_keys: list[str] = []


class OfferIn(BaseModel):
    buyer_name: str
    buyer_email: str
    amount: int = Field(gt=0)
    message: str = ""


class DecisionIn(BaseModel):
    seller_email: str
    decision: Literal["accepted", "rejected"]


# ---------- helpers ----------
def plain(item: dict) -> dict:
    """DynamoDB returns numbers as Decimal; convert them for JSON."""
    return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items()}


def photo_urls(keys: list[str]) -> list[str]:
    return [s3.generate_presigned_url("get_object", Params={"Bucket": PHOTOS_BUCKET, "Key": k}, ExpiresIn=3600)
            for k in keys]


def public_listing(item: dict) -> dict:
    data = plain(item)
    data.pop("seller_email", None)  # never expose the seller's email publicly
    data["photo_urls"] = photo_urls(data.get("photo_keys", []))
    return data


def get_listing(listing_id: str) -> dict:
    item = listings.get_item(Key={"id": listing_id}).get("Item")
    if not item:
        raise HTTPException(404, "Listing not found")
    return item


def publish(event_type: str, offer: dict, listing: dict) -> None:
    """Send an event to SQS; the worker turns it into an email (async)."""
    body = {
        "type": event_type,
        "offer": plain(offer),
        "listing": {k: listing[k] for k in ("id", "title", "city", "seller_name", "seller_email")},
    }
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body, default=int))


# ---------- endpoints ----------
@app.get("/api/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/api/uploads")
def create_upload_url(req: UploadRequest):
    """Return a presigned URL so the browser uploads the photo straight to S3."""
    key = f"listings/{uuid.uuid4().hex}-{req.filename}"
    url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": PHOTOS_BUCKET, "Key": key, "ContentType": req.content_type}, ExpiresIn=600)
    return {"key": key, "upload_url": url}


@app.get("/api/listings")
def search_listings(city: str = "", property_type: str = "", max_price: int = 0):
    flt = Attr("status").ne("sold")
    if city:
        flt &= Attr("city_lower").contains(city.lower())
    if property_type:
        flt &= Attr("property_type").eq(property_type)
    if max_price:
        flt &= Attr("price").lte(max_price)
    items = listings.scan(FilterExpression=flt)["Items"]  # fine for a demo; use a GSI at scale
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return [public_listing(i) for i in items]


@app.get("/api/listings/{listing_id}")
def listing_details(listing_id: str):
    return public_listing(get_listing(listing_id))


@app.post("/api/listings", status_code=201)
def create_listing(body: ListingIn):
    item = body.model_dump() | {
        "id": uuid.uuid4().hex[:10],
        "seller_email": body.seller_email.lower(),
        "city_lower": body.city.lower(),
        "status": "active",
        "created_at": int(time.time()),
    }
    listings.put_item(Item=item)
    return {"id": item["id"]}


@app.post("/api/listings/{listing_id}/offers", status_code=201)
def make_offer(listing_id: str, body: OfferIn):
    listing = get_listing(listing_id)
    offer = body.model_dump() | {
        "id": uuid.uuid4().hex[:10],
        "buyer_email": body.buyer_email.lower(),
        "listing_id": listing_id,
        "status": "pending",
        "created_at": int(time.time()),
    }
    offers.put_item(Item=offer)
    publish("OfferMade", offer, listing)  # -> email to seller
    return {"id": offer["id"], "status": "pending"}


@app.post("/api/offers/{offer_id}/decision")
def decide_offer(offer_id: str, body: DecisionIn):
    offer = offers.get_item(Key={"id": offer_id}).get("Item")
    if not offer:
        raise HTTPException(404, "Offer not found")
    listing = get_listing(offer["listing_id"])
    if listing["seller_email"].lower() != body.seller_email.lower():
        raise HTTPException(403, "Only the seller can decide on this offer")
    if offer["status"] != "pending":
        raise HTTPException(409, f"Offer already {offer['status']}")

    offer["status"] = body.decision
    offers.update_item(Key={"id": offer_id}, UpdateExpression="SET #s = :s",
                       ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": body.decision})
    if body.decision == "accepted":
        listings.update_item(Key={"id": listing["id"]}, UpdateExpression="SET #s = :s",
                             ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "under_offer"})
    publish("OfferAccepted" if body.decision == "accepted" else "OfferRejected", offer, listing)  # -> email to buyer
    return {"id": offer_id, "status": body.decision}


@app.get("/api/dashboard")
def dashboard(email: str):
    """Seller view (own listings + offers received) and buyer view (offers made).

    Demo shortcut: identified by email in the link. A real version would use a signed token.
    """
    email = email.lower()
    my_listings = [plain(i) for i in listings.scan(FilterExpression=Attr("seller_email").eq(email))["Items"]]
    ids = [l["id"] for l in my_listings]
    received = [plain(o) for o in offers.scan(FilterExpression=Attr("listing_id").is_in(ids))["Items"]] if ids else []
    for l in my_listings:
        l["offers"] = sorted([o for o in received if o["listing_id"] == l["id"]], key=lambda o: -o["created_at"])
        l["photo_urls"] = photo_urls(l.get("photo_keys", []))

    made = [plain(o) for o in offers.scan(FilterExpression=Attr("buyer_email").eq(email))["Items"]]
    titles = {}
    for o in made:
        if o["listing_id"] not in titles:
            item = listings.get_item(Key={"id": o["listing_id"]}).get("Item") or {}
            titles[o["listing_id"]] = item.get("title", "(removed)")
        o["listing_title"] = titles[o["listing_id"]]
    return {"listings": my_listings, "offers_made": sorted(made, key=lambda o: -o["created_at"])}
