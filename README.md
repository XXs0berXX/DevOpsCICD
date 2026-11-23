# NestMarket

Minimal property buy/sell site: 3-tier + event-driven, built for AWS (S3, EC2, DynamoDB, SQS, SES)
but runnable entirely on your machine.

```
code/
├── frontend/                  5 static pages (served by S3 in AWS)
│   ├── index.html             Browse + filters
│   ├── listing.html           Listing details
│   ├── offer.html             Make an offer
│   ├── sell.html              Post a listing (photos go straight to S3 via presigned URL)
│   ├── dashboard.html         Seller: accept/reject offers · Buyer: offer status
│   ├── app.js / style.css     Shared helpers and styles
│   └── config.js              API address (overwritten by CI/CD on deploy)
├── api/                       FastAPI REST API  -> Docker image -> EC2
├── worker/                    SQS consumer that sends emails via SES -> Docker image -> EC2
├── infra/local_setup.py       Creates tables, bucket, queue + DLQ, SES sender (local)
├── infra/aws_setup.py         Creates all real AWS resources + GitHub OIDC role
├── deploy/ec2_deploy.sh       Runs on EC2: docker compose up from the S3 build
├── docker-compose.yml         Runs api + worker on EC2
├── .github/workflows/deploy.yml   CI/CD pipeline
└── dev.py                     Runs everything locally
```

## Run locally (no AWS account, no Docker)

```bash
cd code
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
python dev.py
```

Then open:

| What | URL |
|---|---|
| Website | http://localhost:8080 |
| API docs (try every endpoint) | http://localhost:8000/docs |
| Fake AWS (moto dashboard) | http://localhost:5000/moto-api/ |

**How it works locally:** [moto](https://github.com/getmoto/moto) emulates S3, SQS, DynamoDB and SES.
`dev.py` sets `AWS_ENDPOINT_URL=http://localhost:5000`, which boto3 picks up automatically, so the
**same code** that runs on AWS talks to moto instead. Emails are not delivered; the worker prints
each one in the terminal. Data is in memory and resets when you stop `dev.py`.

### Try the full flow

1. **Sell** → post a listing with a photo (seller email e.g. `seller@test.com`).
2. **Browse** → open it → **Make an Offer** (buyer email e.g. `buyer@test.com`).
3. Terminal: the worker prints *"New offer on …"* to the seller, with a dashboard link.
4. Open the seller dashboard link → **Accept**.
5. Terminal: the worker prints *"Your offer … was accepted"* to the buyer.

## Tests

```bash
pytest api/tests worker/tests -q
```

The API tests use moto's in-process mock, so they also run in CI without AWS.

## Request flow (matches the architecture diagram)

1. Browser loads pages from the frontend (S3 website / `localhost:8080`).
2. Browser calls the REST API (`/api/...`).
3. Browser uploads/views photos in S3 using presigned URLs from `POST /api/uploads`.
4. API reads/writes DynamoDB tables `nestmarket-listings` and `nestmarket-offers`.
5. On offer made / accepted / rejected, the API sends an event to SQS `nestmarket-offer-events`.
6. Worker long-polls SQS. A message that fails 3 times moves to `nestmarket-offer-events-dlq`.
7. Worker sends the email with SES.
8. The email links to the user's dashboard.

## CI/CD pipeline (GitHub Actions -> S3 -> EC2)

`.github/workflows/deploy.yml` runs on every push to `main`:

```
git push -> 1. Unit tests -> 2. Build build-v1.0.<run>.zip -> upload to S3 -> 3. Deploy latest build from S3 -> 4. Verify production
```

| Stage | What happens |
|---|---|
| 1. Unit tests | `pytest api/tests worker/tests`. A failing test stops the pipeline: nothing is built or deployed. |
| 2. Build | Writes the version (`1.0.<run number>`) into `api/VERSION` and `frontend/config.js`, zips `api/ worker/ frontend/ deploy/ docker-compose.yml` into `build-v1.0.<run>.zip`, uploads it to `s3://<ARTIFACT_BUCKET>/builds/` and copies it to `builds/latest.zip`. |
| 3. Deploy | Downloads `builds/latest.zip` from S3. Frontend -> S3 static website bucket. Backend -> EC2 via SSM: the instance downloads the same zip from S3 and runs `deploy/ec2_deploy.sh` (`docker compose up -d --build`). |
| 4. Verify | Checks that `API_URL/api/health` reports the new version and the website serves the new `config.js`. |

The deployed version is shown in the website footer (`NestMarket � build v1.0.N`) and in `GET /api/health`.

GitHub logs in to AWS with OIDC (role `nestmarket-github-deploy`, only this repository can assume it), so no AWS keys are stored in GitHub.

### One-time AWS setup

`infra/aws_setup.py` creates everything (run it in AWS CloudShell: `python3 aws_setup.py <owner>/<repo>`):
DynamoDB tables, SQS queue + DLQ, S3 buckets (artifacts, website, photos), EC2 t3.micro with Docker, an IAM role,
security group (port 80) and Elastic IP, and the GitHub OIDC provider + deploy role. It prints the repository variables
to set under Settings > Secrets and variables > Actions > Variables:
`AWS_REGION, AWS_ROLE_ARN, ARTIFACT_BUCKET, WEBSITE_BUCKET, EC2_INSTANCE_ID, API_URL, WEBSITE_URL`.

SES is in sandbox mode: to receive real emails, verify the sender and recipient addresses in SES and set
`SENDER_EMAIL` in `/opt/nestmarket/.env` on the instance.
