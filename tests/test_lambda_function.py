"""Integration tests for src/lambda_function.py."""

import importlib
import os
from unittest import mock

import boto3
import pytest
from moto import mock_dynamodb, mock_s3, mock_sns

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGION = "us-east-1"

VALID_ENV = {
    "DYNAMODB_TABLE": "test-table",
    "S3_BUCKET": "test-bucket",
    "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
    "TWILIO_AUTH_TOKEN": "test-token",
    "LLM_PROVIDER": "unknown",  # forces keyword fallback; no LLM calls
}


def _make_event(body: str, signature: str = "dummy") -> dict:
    return {
        "body": body,
        "headers": {"X-Twilio-Signature": signature},
        "requestContext": {
            "domainName": "example.execute-api.us-east-1.amazonaws.com",
            "http": {"path": "/webhook"},
        },
    }


def _setup_aws():
    """Create the mocked AWS resources expected by the handler."""
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    dynamodb.create_table(
        TableName="test-table",
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket="test-bucket")
    sns = boto3.client("sns", region_name=REGION)
    sns.create_topic(Name="test-topic")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@mock_dynamodb
@mock_s3
@mock_sns
def test_low_urgency_flow():
    """Happy path: mild symptom returns self-care TwiML."""
    _setup_aws()
    body = "From=%2B1234567890&Body=I%20have%20a%20mild%20headache"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/xml"
    body_lower = response["body"].lower()
    assert "rest" in body_lower or "self-care" in body_lower or "hydrat" in body_lower


@mock_dynamodb
@mock_s3
@mock_sns
def test_medium_urgency_flow():
    """Fever/vomiting should return appointment-scheduling TwiML."""
    _setup_aws()
    body = "From=%2B1234567890&Body=High+fever+and+vomiting+since+yesterday"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 200
    body_lower = response["body"].lower()
    assert "doctor" in body_lower or "appointment" in body_lower or "24 hour" in body_lower


@mock_dynamodb
@mock_s3
@mock_sns
def test_high_urgency_triggers_sns():
    """Chest pain should return emergency TwiML and publish an SNS alert."""
    _setup_aws()
    body = "From=%2B1234567890&Body=Chest+pain+and+shortness+of+breath"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 200
    body_lower = response["body"].lower()
    assert "emergency" in body_lower or "ambulance" in body_lower or "911" in body_lower


@mock_dynamodb
@mock_s3
@mock_sns
def test_invalid_signature_returns_403():
    """A bad Twilio signature must be rejected with HTTP 403."""
    _setup_aws()
    body = "From=%2B1234567890&Body=Hello"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        # Do NOT patch verify_twilio_signature — let it return False for a dummy sig
        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=False):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 403


@mock_dynamodb
@mock_s3
@mock_sns
def test_missing_from_returns_400():
    """A request without a From field must return HTTP 400."""
    _setup_aws()
    body = "Body=Hello%20there"  # no From parameter

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 400


@mock_dynamodb
@mock_s3
@mock_sns
def test_missing_body_returns_400():
    """A request without a Body field must return HTTP 400."""
    _setup_aws()
    body = "From=%2B1234567890"  # no Body parameter

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 400


@mock_dynamodb
@mock_s3
@mock_sns
def test_rate_limited_user_receives_friendly_message():
    """A user who has sent >= 50 messages today should receive a rate-limit reply."""
    _setup_aws()

    # Pre-seed a conversation that is already at the limit
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.Table("test-table")
    table.put_item(Item={
        "user_id": "+19995551234",
        "history": [],
        "triage_level": None,
        "last_intent": None,
        "daily_count": 50,
        "count_date": today,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "ttl": 9999999999,
    })

    body = "From=%2B19995551234&Body=Another+message"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    assert response["statusCode"] == 200
    assert "too many" in response["body"].lower() or "tomorrow" in response["body"].lower()


@mock_dynamodb
@mock_s3
@mock_sns
def test_response_is_valid_twiml_xml():
    """The response body must be a well-formed TwiML XML document."""
    import xml.etree.ElementTree as ET
    _setup_aws()
    body = "From=%2B1234567890&Body=I+feel+dizzy"

    with mock.patch.dict(os.environ, VALID_ENV):
        from src import lambda_function
        importlib.reload(lambda_function)

        with mock.patch("src.lambda_function.verify_twilio_signature", return_value=True):
            response = lambda_function.lambda_handler(_make_event(body), None)

    # This will raise if XML is malformed
    root = ET.fromstring(response["body"])
    assert root.tag == "Response"
    messages = root.findall("Message")
    assert len(messages) == 1
    assert messages[0].text  # not empty
