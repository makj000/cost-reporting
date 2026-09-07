import datetime
import os

import boto3
from boto3.dynamodb.conditions import Key

CE_REGION = "us-east-1"
SES_REGION = "us-west-2"
SENDER = os.environ["SENDER_EMAIL"]
RECIPIENT = os.environ["RECIPIENT_EMAIL"]
DIGEST_TABLE_NAME = os.environ["DIGEST_TABLE_NAME"]


def _collect_digest_sections(week_start):
    table = boto3.resource("dynamodb").Table(DIGEST_TABLE_NAME)
    response = table.query(KeyConditionExpression=Key("week_start").eq(week_start))
    return [(item["project"], item["section"]) for item in response["Items"]]


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

    subject = f"AWS weekly cost report: ${usage:.2f} usage, ${actual_charge:.2f} charged ({start.isoformat()} to {end.isoformat()})"
    body = (
        f"AWS cost for {start.isoformat()} to {end.isoformat()}:\n\n"
        f"Usage cost (before credits): ${usage:.2f}\n"
        f"Credits applied: ${credit:.2f}\n"
        f"Actual charge (after credits): ${actual_charge:.2f}\n"
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

    return {"statusCode": 200, "usage": usage, "credit": credit, "actual_charge": actual_charge}
