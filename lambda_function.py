import datetime
import json
import os
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key

CE_REGION = "us-east-1"
SES_REGION = "us-west-2"
BILLING_ENDPOINT = "https://billing.us-east-1.api.aws"
SENDER = os.environ["SENDER_EMAIL"]
RECIPIENT = os.environ["RECIPIENT_EMAIL"]
DIGEST_TABLE_NAME = os.environ["DIGEST_TABLE_NAME"]
CREDIT_LOOKBACK_DAYS = 365


def _collect_digest_sections(week_start):
    table = boto3.resource("dynamodb").Table(DIGEST_TABLE_NAME)
    response = table.query(KeyConditionExpression=Key("week_start").eq(week_start))
    return [(item["project"], item["section"]) for item in response["Items"]]


def _amount_value(amount):
    return float(amount["currencyAmount"])


def _epoch_seconds(value):
    date_time = datetime.datetime.combine(value, datetime.time.min, tzinfo=datetime.timezone.utc)
    return int(date_time.timestamp())


def _billing_request(operation_name, payload):
    session = boto3.session.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    readonly_credentials = ReadOnlyCredentials(
        credentials.access_key,
        credentials.secret_key,
        credentials.token,
    )
    data = json.dumps(payload).encode("utf-8")
    request = AWSRequest(
        method="POST",
        url=BILLING_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/x-amz-json-1.0",
            "X-Amz-Target": f"AWSBilling.{operation_name}",
        },
    )
    SigV4Auth(readonly_credentials, "billing", CE_REGION).add_auth(request)

    prepared = request.prepare()
    http_request = urllib.request.Request(
        prepared.url,
        data=prepared.body,
        headers=dict(prepared.headers.items()),
        method=prepared.method,
    )
    with urllib.request.urlopen(http_request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _collect_credit_balance(end):
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    start = end - datetime.timedelta(days=CREDIT_LOOKBACK_DAYS)
    response = _billing_request(
        "GetCredits",
        {
            "accountId": account_id,
            "startDate": _epoch_seconds(start),
            "endDate": _epoch_seconds(end),
            "payerAccountFlag": True,
        },
    )

    estimated_left = 0.0
    last_cycle_left = 0.0
    enabled_count = 0
    for credit in response["credits"]:
        if credit.get("creditStatus") != "ENABLED":
            continue
        enabled_count += 1
        estimated_left += _amount_value(credit.get("estimatedAmount") or credit["remainingAmount"])
        last_cycle_left += _amount_value(credit["remainingAmount"])

    return {
        "estimated_left": estimated_left,
        "last_cycle_left": last_cycle_left,
        "enabled_count": enabled_count,
    }


def _format_credit_balance(credit_balance):
    if credit_balance is None:
        return "Credit balance left: unavailable\n"

    return (
        f"Credit balance left (estimated): ${credit_balance['estimated_left']:.2f}\n"
        f"Credit balance left (last billing cycle): ${credit_balance['last_cycle_left']:.2f}\n"
        f"Enabled credits: {credit_balance['enabled_count']}\n"
    )


def lambda_handler(event, context):
    ce = boto3.client("ce", region_name=CE_REGION)
    end = datetime.date.today()
    start = end - datetime.timedelta(days=7)

    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
    )

    by_type = {}
    for day in response["ResultsByTime"]:
        for group in day["Groups"]:
            record_type = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            by_type[record_type] = by_type.get(record_type, 0.0) + amount

    usage = sum(amount for record_type, amount in by_type.items() if record_type != "Credit")
    credit = -by_type.get("Credit", 0.0)
    actual_charge = usage - credit
    try:
        credit_balance = _collect_credit_balance(end)
    except (AttributeError, BotoCoreError, ClientError, KeyError, ValueError, urllib.error.URLError) as error:
        detail = error.read().decode("utf-8") if isinstance(error, urllib.error.HTTPError) else str(error)
        print(f"Credit balance unavailable: {type(error).__name__}: {detail}")
        credit_balance = None

    subject = f"AWS weekly cost report: ${usage:.2f} usage, ${actual_charge:.2f} charged ({start.isoformat()} to {end.isoformat()})"
    body = (
        f"AWS cost for {start.isoformat()} to {end.isoformat()}:\n\n"
        f"Usage cost (before credits): ${usage:.2f}\n"
        f"Credits applied: ${credit:.2f}\n"
        f"Actual charge (after credits): ${actual_charge:.2f}\n"
        f"{_format_credit_balance(credit_balance)}"
    )

    digest_sections = _collect_digest_sections(end.isoformat())
    for project, section in digest_sections:
        body += f"\n\n--- {project} ---\n{section}\n"

    if digest_sections:
        subject = f"Weekly Summary — {end.isoformat()}: ${usage:.2f} AWS cost, {len(digest_sections)} project update(s)"

    ses = boto3.client("ses", region_name=SES_REGION)
    ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )

    return {
        "statusCode": 200,
        "usage": usage,
        "credit": credit,
        "actual_charge": actual_charge,
        "credit_balance": credit_balance,
    }
