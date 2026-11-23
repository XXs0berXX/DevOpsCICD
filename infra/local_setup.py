"""Create NestMarket's AWS resources (tables, bucket, queues, SES sender).

Locally this runs against the moto emulator (AWS_ENDPOINT_URL=http://localhost:5000).
The same calls show what you would create once in real AWS (via console or CLI).
Safe to run more than once.
"""
import json
import os

import boto3

REGION = os.getenv("AWS_REGION", "us-east-1")
LISTINGS_TABLE = os.getenv("LISTINGS_TABLE", "nestmarket-listings")
OFFERS_TABLE = os.getenv("OFFERS_TABLE", "nestmarket-offers")
PHOTOS_BUCKET = os.getenv("PHOTOS_BUCKET", "nestmarket-photos")
QUEUE_NAME = os.getenv("QUEUE_NAME", "nestmarket-offer-events")
DLQ_NAME = QUEUE_NAME + "-dlq"
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "no-reply@nestmarket.local")


def create_table(ddb, name: str) -> None:
    if name in ddb.list_tables()["TableNames"]:
        return
    # Provisioned 5/5 stays inside DynamoDB's always-free 25 RCU / 25 WCU.
    ddb.create_table(TableName=name, KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                     ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5})
    print(f"  table   {name}")


def main() -> None:
    ddb = boto3.client("dynamodb", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)
    ses = boto3.client("ses", region_name=REGION)

    print("Creating resources...")
    create_table(ddb, LISTINGS_TABLE)
    create_table(ddb, OFFERS_TABLE)

    s3.create_bucket(Bucket=PHOTOS_BUCKET)
    s3.put_bucket_cors(Bucket=PHOTOS_BUCKET, CORSConfiguration={"CORSRules": [{
        "AllowedMethods": ["GET", "PUT"], "AllowedOrigins": ["*"], "AllowedHeaders": ["*"]}]})
    print(f"  bucket  {PHOTOS_BUCKET} (CORS for browser uploads)")

    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.create_queue(QueueName=QUEUE_NAME, Attributes={
        "VisibilityTimeout": "30",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"}),
    })
    print(f"  queue   {QUEUE_NAME} -> DLQ {DLQ_NAME} after 3 failed tries")

    ses.verify_email_identity(EmailAddress=SENDER_EMAIL)  # SES only sends from verified senders
    print(f"  ses     verified sender {SENDER_EMAIL}")


if __name__ == "__main__":
    main()
