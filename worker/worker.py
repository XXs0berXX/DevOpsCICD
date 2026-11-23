"""NestMarket email worker - consumes offer events from SQS and sends emails with SES.

If sending fails, the message is NOT deleted. SQS makes it visible again and retries it;
after 3 failed receives the queue's redrive policy moves it to the dead-letter queue.
"""
import json
import logging
import os
import time

import boto3

REGION = os.getenv("AWS_REGION", "us-east-1")
QUEUE_NAME = os.getenv("QUEUE_NAME", "nestmarket-offer-events")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "no-reply@nestmarket.local")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8080")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
log = logging.getLogger()


def build_email(event: dict, frontend_url: str = FRONTEND_URL) -> tuple[str, str, str]:
    """Turn an event into (to, subject, body). Pure function, so it is easy to unit-test."""
    offer, listing = event["offer"], event["listing"]
    amount = f"{offer['amount']:,}"

    if event["type"] == "OfferMade":
        to = listing["seller_email"]
        link = f"{frontend_url}/dashboard.html?email={to}"
        subject = f"New offer on {listing['title']}"
        body = (f"Hi {listing['seller_name']},\n\n"
                f"{offer['buyer_name']} offered {amount} for \"{listing['title']}\" ({listing['city']}).\n"
                f"Message: {offer.get('message') or '-'}\n\n"
                f"Accept or reject it on your dashboard:\n{link}\n")
    elif event["type"] in ("OfferAccepted", "OfferRejected"):
        to = offer["buyer_email"]
        link = f"{frontend_url}/dashboard.html?email={to}"
        decision = "accepted" if event["type"] == "OfferAccepted" else "rejected"
        subject = f"Your offer on {listing['title']} was {decision}"
        body = (f"Hi {offer['buyer_name']},\n\n"
                f"Your offer of {amount} for \"{listing['title']}\" was {decision} by the seller.\n"
                + ("The seller will contact you to arrange the next steps.\n" if decision == "accepted" else "")
                + f"\nSee your offers:\n{link}\n")
    else:
        raise ValueError(f"Unknown event type: {event['type']}")
    return to, subject, body


def main() -> None:
    sqs = boto3.client("sqs", region_name=REGION)
    ses = boto3.client("ses", region_name=REGION)

    queue_url = None
    while queue_url is None:  # wait until the queue exists (e.g. local setup still running)
        try:
            queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
        except Exception:
            log.info("waiting for queue %s ...", QUEUE_NAME)
            time.sleep(2)
    log.info("listening on %s", queue_url)

    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20)  # long poll
        for msg in resp.get("Messages", []):
            try:
                event = json.loads(msg["Body"])
                to, subject, body = build_email(event)
                ses.send_email(
                    Source=SENDER_EMAIL,
                    Destination={"ToAddresses": [to]},
                    Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
                )
                log.info("EMAIL SENT\n  To: %s\n  Subject: %s\n  ---\n%s", to, subject,
                         "\n".join("  " + line for line in body.splitlines()))
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
            except Exception:
                log.exception("failed to process message %s (will be retried, then sent to DLQ)", msg["MessageId"])


if __name__ == "__main__":
    main()
