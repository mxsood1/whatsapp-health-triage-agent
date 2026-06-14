"""Tests for src/utils.py — triage logic, rate limiting, DynamoDB helpers."""

import json
import time
from datetime import datetime, timezone
from unittest import mock

import boto3
import pytest
from moto import mock_dynamodb, mock_s3, mock_sns

from src.utils import (
    _fallback_classifier,
    _mask,
    build_response_and_state,
    classify_message,
    generate_twiml_response,
    increment_message_count,
    is_rate_limited,
    load_conversation,
    sanitize_input,
    send_alert,
    store_conversation,
    upload_transcript,
)


# ---------------------------------------------------------------------------
# sanitize_input
# ---------------------------------------------------------------------------

def test_sanitize_strips_whitespace():
    assert sanitize_input("  hello  ") == "hello"


def test_sanitize_truncates_long_input():
    long_msg = "x" * 2000
    result = sanitize_input(long_msg)
    assert len(result) == 1600


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_is_rate_limited_new_user():
    conv = {}
    assert is_rate_limited(conv) is False
    assert conv["daily_count"] == 0


def test_is_rate_limited_exceeds_threshold():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conv = {"count_date": today, "daily_count": 50}
    assert is_rate_limited(conv) is True


def test_is_rate_limited_resets_on_new_day():
    conv = {"count_date": "2000-01-01", "daily_count": 999}
    assert is_rate_limited(conv) is False
    assert conv["daily_count"] == 0


def test_increment_message_count():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conv = {"count_date": today, "daily_count": 5}
    increment_message_count(conv)
    assert conv["daily_count"] == 6


def test_increment_message_count_new_day():
    conv = {"count_date": "2000-01-01", "daily_count": 99}
    increment_message_count(conv)
    assert conv["daily_count"] == 1


# ---------------------------------------------------------------------------
# Fallback classifier
# ---------------------------------------------------------------------------

def test_fallback_low():
    result = _fallback_classifier("I have a mild headache.")
    assert result["urgency"] == "LOW"


def test_fallback_medium_fever():
    result = _fallback_classifier("My child has a high fever and is vomiting.")
    assert result["urgency"] == "MEDIUM"


def test_fallback_high_chest_pain():
    result = _fallback_classifier("Sudden chest pain and shortness of breath.")
    assert result["urgency"] == "HIGH"
    assert "chest pain" in result["red_flags"]


def test_fallback_high_unconscious():
    result = _fallback_classifier("The patient is unconscious and not breathing.")
    assert result["urgency"] == "HIGH"


# ---------------------------------------------------------------------------
# classify_message — fallback path (unknown provider)
# ---------------------------------------------------------------------------

def test_classify_unknown_provider_returns_fallback():
    result = classify_message("I have a mild cold.", history=[], provider="unknown")
    assert result["urgency"] == "LOW"


def test_classify_medium_via_fallback():
    result = classify_message("I have a severe pain in my side.", history=[], provider="unknown")
    assert result["urgency"] == "MEDIUM"


def test_classify_high_via_fallback():
    result = classify_message("Chest pain and difficulty breathing.", history=[], provider="unknown")
    assert result["urgency"] == "HIGH"


# ---------------------------------------------------------------------------
# generate_twiml_response — XML escaping
# ---------------------------------------------------------------------------

def test_twiml_wraps_message():
    twiml = generate_twiml_response("Hello!")
    assert "<Message>Hello!</Message>" in twiml
    assert '<?xml version="1.0"' in twiml


def test_twiml_escapes_ampersand():
    twiml = generate_twiml_response("Rest & hydrate.")
    content = twiml.split("<Message>")[1].split("</Message>")[0]
    assert content == "Rest &amp; hydrate."


def test_twiml_escapes_angle_brackets():
    twiml = generate_twiml_response("See <doctor>.")
    assert "&lt;doctor&gt;" in twiml


# ---------------------------------------------------------------------------
# build_response_and_state
# ---------------------------------------------------------------------------

@mock_sns
def test_build_response_high_sends_alert_and_updates_state():
    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="alerts")["TopicArn"]

    triage = {"urgency": "HIGH", "symptoms": ["chest pain"], "red_flags": ["chest pain"], "duration": "10 min"}
    conv = {"history": []}

    reply = build_response_and_state(triage, conv, "chest pain", sns, topic_arn, "+10001234")

    assert "emergency" in reply.lower()
    assert conv["triage_level"] == "HIGH"
    # Both patient message and agent reply appended
    assert len(conv["history"]) == 2
    assert conv["history"][0]["role"] == "patient"
    assert conv["history"][1]["role"] == "agent"


@mock_sns
def test_build_response_medium():
    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="alerts")["TopicArn"]

    triage = {"urgency": "MEDIUM", "symptoms": ["fever"], "red_flags": [], "duration": "2 days"}
    conv = {"history": []}

    reply = build_response_and_state(triage, conv, "high fever", sns, topic_arn, "+10001234")

    assert "doctor" in reply.lower() or "appointment" in reply.lower()
    assert conv["triage_level"] == "MEDIUM"


@mock_sns
def test_build_response_low():
    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="alerts")["TopicArn"]

    triage = {"urgency": "LOW", "symptoms": ["runny nose"], "red_flags": [], "duration": "1 day"}
    conv = {"history": []}

    reply = build_response_and_state(triage, conv, "runny nose", sns, topic_arn, "+10001234")

    assert "self-care" in reply.lower() or "hydrate" in reply.lower() or "rest" in reply.lower()
    assert conv["triage_level"] == "LOW"


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

@mock_dynamodb
def test_load_conversation_returns_default_for_new_user():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="triage",
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    conv = load_conversation(table, "+19995550001")
    assert conv["user_id"] == "+19995550001"
    assert conv["history"] == []
    assert conv["triage_level"] is None


@mock_dynamodb
def test_store_and_reload_conversation():
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="triage",
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    conv = {"user_id": "+19995550001", "history": [], "triage_level": "LOW"}
    store_conversation(table, conv)

    # TTL should be set
    assert "ttl" in conv
    assert conv["ttl"] > int(time.time())

    reloaded = load_conversation(table, "+19995550001")
    assert reloaded["triage_level"] == "LOW"


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

@mock_s3
def test_upload_transcript_creates_object():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")

    upload_transcript(s3, "test-bucket", "+10001234567", "Patient: hello\nAgent: hi")

    objects = s3.list_objects_v2(Bucket="test-bucket")["Contents"]
    assert len(objects) == 1
    assert "transcript_" in objects[0]["Key"]


# ---------------------------------------------------------------------------
# _mask helper
# ---------------------------------------------------------------------------

def test_mask_phone_number():
    assert _mask("+12125550001") == "***0001"
    assert _mask("whatsapp:+12125550001") == "***0001"


def test_mask_short_id():
    assert _mask("123") == "****"
