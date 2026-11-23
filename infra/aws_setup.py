"""One-time AWS setup for NestMarket + its CI/CD pipeline. Run it in AWS CloudShell:

    python3 aws_setup.py <github-owner>/<repo>

Creates (safe to re-run, existing resources are reused):
  data      DynamoDB tables, SQS queue + dead-letter queue, S3 photos bucket
  CI/CD     S3 artifacts bucket (build zips), S3 static website bucket (frontend)
  server    EC2 t3.micro (Amazon Linux 2023 + Docker) with an IAM role, security group (port 80), Elastic IP
  GitHub    OIDC identity provider + deploy role that only this repo can assume (no AWS keys in GitHub)
Prints the GitHub repository variables the workflow needs.
"""
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
REPO = sys.argv[1] if len(sys.argv) > 1 else "XXs0berXX/DevOpsCICD"
P = "nestmarket"

session = boto3.Session(region_name=REGION)
ACCOUNT = session.client("sts").get_caller_identity()["Account"]
ddb, s3, sqs, iam, ec2, ssm = (session.client(n) for n in ("dynamodb", "s3", "sqs", "iam", "ec2", "ssm"))

ARTIFACTS, WEBSITE, PHOTOS = (f"{P}-{kind}-{ACCOUNT}" for kind in ("artifacts", "web", "photos"))
WEBSITE_URL = f"http://{WEBSITE}.s3-website-{REGION}.amazonaws.com"
TABLES = [f"{P}-listings", f"{P}-offers"]
QUEUE, DLQ = f"{P}-offer-events", f"{P}-offer-events-dlq"
EC2_ROLE, DEPLOY_ROLE = f"{P}-ec2-role", f"{P}-github-deploy"
OIDC_URL = "token.actions.githubusercontent.com"


def ok_if_exists(fn, *codes, **kw):
    try:
        return fn(**kw)
    except ClientError as e:
        if e.response["Error"]["Code"] not in codes:
            raise


def doc(*statements):
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)})


def allow(actions, resources):
    return {"Effect": "Allow", "Action": actions, "Resource": resources}


def role(name, trust, inline, managed=()):
    ok_if_exists(iam.create_role, "EntityAlreadyExists", RoleName=name, AssumeRolePolicyDocument=trust)
    iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-policy", PolicyDocument=inline)
    for arn in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=arn)
    return iam.get_role(RoleName=name)["Role"]["Arn"]


# ---------- data tier ----------
for t in TABLES:
    ok_if_exists(ddb.create_table, "ResourceInUseException", TableName=t,
                 KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                 AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                 ProvisionedThroughput={"ReadCapacityUnits": 5, "WriteCapacityUnits": 5})  # inside always-free 25/25
print("tables  ", TABLES)

dlq_url = sqs.create_queue(QueueName=DLQ)["QueueUrl"]
dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
sqs.create_queue(QueueName=QUEUE, Attributes={
    "VisibilityTimeout": "30",
    "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "3"})})
print("queues  ", QUEUE, "->", DLQ)

# ---------- S3 buckets ----------
for b in (ARTIFACTS, WEBSITE, PHOTOS):
    ok_if_exists(s3.create_bucket, "BucketAlreadyOwnedByYou", Bucket=b)
s3.put_bucket_cors(Bucket=PHOTOS, CORSConfiguration={"CORSRules": [{
    "AllowedMethods": ["GET", "PUT"], "AllowedOrigins": ["*"], "AllowedHeaders": ["*"]}]})
s3.put_bucket_lifecycle_configuration(Bucket=ARTIFACTS, LifecycleConfiguration={"Rules": [{
    "ID": "expire-old-builds", "Status": "Enabled", "Filter": {"Prefix": "builds/"}, "Expiration": {"Days": 60}}]})
s3.put_public_access_block(Bucket=WEBSITE, PublicAccessBlockConfiguration={
    "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": False, "RestrictPublicBuckets": False})
s3.put_bucket_website(Bucket=WEBSITE, WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
s3.put_bucket_policy(Bucket=WEBSITE, Policy=doc(
    {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{WEBSITE}/*"}))
print("buckets ", ARTIFACTS, WEBSITE, PHOTOS)

# ---------- EC2 instance role ----------
ec2_role_arn = role(
    EC2_ROLE,
    doc({"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}),
    doc(allow(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
               "dynamodb:Scan", "dynamodb:Query"], [f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{t}" for t in TABLES]),
        allow(["s3:GetObject", "s3:PutObject"], [f"arn:aws:s3:::{PHOTOS}/*"]),
        allow(["s3:GetObject"], [f"arn:aws:s3:::{ARTIFACTS}/*"]),
        allow(["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueUrl",
               "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], [f"arn:aws:sqs:{REGION}:{ACCOUNT}:{P}-*"]),
        allow(["ses:SendEmail", "ses:SendRawEmail"], "*")),
    managed=("arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",      # lets the pipeline run commands via SSM
             "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"))      # container logs -> CloudWatch
if ok_if_exists(iam.create_instance_profile, "EntityAlreadyExists", InstanceProfileName=EC2_ROLE):
    iam.add_role_to_instance_profile(InstanceProfileName=EC2_ROLE, RoleName=EC2_ROLE)
    time.sleep(15)  # IAM propagation before EC2 can use the profile
print("ec2 role", ec2_role_arn)

# ---------- EC2 instance ----------
vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{P}-web"]}])["SecurityGroups"]
if sgs:
    sg = sgs[0]["GroupId"]
else:
    sg = ec2.create_security_group(GroupName=f"{P}-web", Description="NestMarket API (HTTP)", VpcId=vpc)["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])

ENV_FILE = "\n".join([
    f"AWS_REGION={REGION}", f"AWS_DEFAULT_REGION={REGION}",
    f"LISTINGS_TABLE={TABLES[0]}", f"OFFERS_TABLE={TABLES[1]}", f"PHOTOS_BUCKET={PHOTOS}", f"QUEUE_NAME={QUEUE}",
    f"FRONTEND_URL={WEBSITE_URL}", "CORS_ORIGINS=*", "SENDER_EMAIL=no-reply@example.com"])
USER_DATA = f"""#!/bin/bash
set -eux
dnf install -y docker unzip
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 \\
  -o /usr/local/lib/docker/cli-plugins/docker-compose
curl -fsSL https://github.com/docker/buildx/releases/download/v0.17.1/buildx-v0.17.1.linux-amd64 \\
  -o /usr/local/lib/docker/cli-plugins/docker-buildx
chmod +x /usr/local/lib/docker/cli-plugins/*
dd if=/dev/zero of=/swapfile bs=1M count=1024 && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
mkdir -p /opt/nestmarket
cat > /opt/nestmarket/.env <<'EOF'
{ENV_FILE}
EOF
"""

existing = ec2.describe_instances(Filters=[
    {"Name": "tag:Name", "Values": [f"{P}-prod"]},
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
if existing:
    instance_id = existing[0]["Instances"][0]["InstanceId"]
else:
    ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    instance_id = ec2.run_instances(
        ImageId=ami["Parameter"]["Value"], InstanceType="t3.micro", MinCount=1, MaxCount=1,
        SecurityGroupIds=[sg], IamInstanceProfile={"Name": EC2_ROLE}, UserData=USER_DATA,
        MetadataOptions={"HttpTokens": "required", "HttpPutResponseHopLimit": 2},  # containers can reach the role
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3"}}],
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": f"{P}-prod"}]}],
    )["Instances"][0]["InstanceId"]
ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

addrs = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [f"{P}-ip"]}])["Addresses"]
if addrs:
    alloc, ip = addrs[0]["AllocationId"], addrs[0]["PublicIp"]
else:
    a = ec2.allocate_address(Domain="vpc", TagSpecifications=[{
        "ResourceType": "elastic-ip", "Tags": [{"Key": "Name", "Value": f"{P}-ip"}]}])
    alloc, ip = a["AllocationId"], a["PublicIp"]
ec2.associate_address(AllocationId=alloc, InstanceId=instance_id)
print("ec2     ", instance_id, ip)

# ---------- GitHub OIDC + deploy role ----------
oidc_arn = f"arn:aws:iam::{ACCOUNT}:oidc-provider/{OIDC_URL}"
ok_if_exists(iam.create_open_id_connect_provider, "EntityAlreadyExists", Url=f"https://{OIDC_URL}",
             ClientIDList=["sts.amazonaws.com"], ThumbprintList=["6938fd4d98bab03faadb97b34396831e3780aea1"])
deploy_role_arn = role(
    DEPLOY_ROLE,
    doc({"Effect": "Allow", "Principal": {"Federated": oidc_arn}, "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {"StringEquals": {f"{OIDC_URL}:aud": "sts.amazonaws.com"},
                       # classic subject "repo:owner/name:..." and GitHub's immutable one "repo:owner@id/name@id:..."
                       "StringLike": {f"{OIDC_URL}:sub": [f"repo:{REPO}:*", "repo:{}@*/{}@*:*".format(*REPO.split("/"))]}}}),
    doc(allow(["s3:ListBucket"], [f"arn:aws:s3:::{ARTIFACTS}", f"arn:aws:s3:::{WEBSITE}"]),
        allow(["s3:GetObject", "s3:PutObject"], [f"arn:aws:s3:::{ARTIFACTS}/*"]),
        allow(["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], [f"arn:aws:s3:::{WEBSITE}/*"]),
        allow(["ssm:SendCommand"], [f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}",
                                    f"arn:aws:ssm:{REGION}::document/AWS-RunShellScript"]),
        allow(["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], "*")))
print("deploy  ", deploy_role_arn)

variables = {
    "AWS_REGION": REGION, "AWS_ROLE_ARN": deploy_role_arn, "ARTIFACT_BUCKET": ARTIFACTS,
    "WEBSITE_BUCKET": WEBSITE, "EC2_INSTANCE_ID": instance_id, "API_URL": f"http://{ip}", "WEBSITE_URL": WEBSITE_URL}
print("\nGitHub repository variables:")
for k, v in variables.items():
    print(f"  {k}={v}")
