"""AWS Lambda handler for the WhatsApp Healthcare Triage Agent.

Entry point for API Gateway HTTP API events forwarded from Twilio. Validates
the webhook signature, loads/updates conversation state from DynamoDB, calls
the LLM classifier, and returns a TwiML XML response.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List
from urllib.parse import parse_qs

import boto3

# Support both Lambda runtime (CodeUri: src/ → no 'src.' prefix) and
# local pytest (project root → needs 'src.' prefix).
try:
    from utils import (
        build_response_and_state,
        classify_message,
        generate_twiml_response,
        increment_message_count,
        is_rate_limited,
        load_conversation,
        sanitize_input,
        store_conversation,
        upload_transcript,
    )
    import utils as _utils
except ImportError:
    from src.utils import (
        build_response_and_state,
        classify_message,
        generate_twiml_response,
        increment_message_count,
        is_rate_limited,
        load_conversation,
        sanitize_input,
        store_conversation,
        upload_transcript,
    )
    import src.utils as _utils

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients initialised once per container for connection reuse
dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
sns_client = boto3.client("sns")

table_name = os.environ.get("DYNAMODB_TABLE")
bucket_name = os.environ.get("S3_BUCKET")
topic_arn = os.environ.get("SNS_TOPIC_ARN")
twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
llm_provider = os.environ.get("LLM_PROVIDER", "bedrock")

_missing = [
    k for k, v in {
        "DYNAMODB_TABLE": table_name,
        "S3_BUCKET": bucket_name,
        "SNS_TOPIC_ARN": topic_arn,
        "TWILIO_AUTH_TOKEN": twilio_auth_token,
    }.items()
    if not v
]
if _missing:
    logger.warning(json.dumps({"event": "missing_env_vars", "vars": _missing}))

table = dynamodb.Table(table_name) if table_name else None


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Process an incoming Twilio WhatsApp webhook and return a TwiML reply."""
    request_id = getattr(context, "aws_request_id", "local")
    logger.info(json.dumps({"event": "webhook_received", "request_id": request_id}))

    body = event.get("body", "") or ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    signature = headers.get("x-twilio-signature", "")

    # Reconstruct the full webhook URL that Twilio used to sign the request
    rc = event.get("requestContext", {})
    domain = rc.get("domainName", "")
    path = rc.get("http", {}).get("path", "")
    full_url = f"https://{domain}{path}"

    params: Dict[str, List[str]] = parse_qs(body)

    # Use globals() so tests can patch verify_twilio_signature on this module
    verify_fn = globals().get("verify_twilio_signature", _utils.verify_twilio_signature)
    if not verify_fn(signature, full_url, params, twilio_auth_token or ""):
        logger.warning(json.dumps({"event": "invalid_signature", "request_id": request_id}))
        return {"statusCode": 403, "body": "Forbidden"}

    user_id = params.get("From", [""])[0]
    raw_message = params.get("Body", [""])[0].strip()

    if not user_id or not raw_message:
        return {"statusCode": 400, "body": "Missing From or Body parameter"}

    message = sanitize_input(raw_message)
    masked_user = f"***{user_id[-4:]}" if len(user_id) > 4 else "****"

    conversation = load_conversation(table, user_id) if table else {}

    if is_rate_limited(conversation):
        logger.warning(json.dumps({"event": "rate_limited", "user": masked_user}))
        twiml = generate_twiml_response(
            "You have sent too many messages today. Please try again tomorrow "
            "or contact your healthcare provider directly."
        )
        return {"statusCode": 200, "headers": {"Content-Type": "application/xml"}, "body": twiml}

    increment_message_count(conversation)

    triage_result = classify_message(
        message,
        conversation.get("history", []),
        provider=llm_provider,
    )

    logger.info(json.dumps({
        "event": "triage_complete",
        "request_id": request_id,
        "user": masked_user,
        "urgency": triage_result.get("urgency"),
        "provider": llm_provider,
    }))

    reply_message = build_response_and_state(
        triage_result, conversation, message, sns_client, topic_arn, user_id
    )

    if table:
        store_conversation(table, conversation)

    if bucket_name:
        lines = [
            f"{msg['timestamp']} [{msg.get('role', 'patient')}]: {msg['message']}"
            for msg in conversation.get("history", [])
        ]
        upload_transcript(s3_client, bucket_name, user_id, "\n".join(lines))

    twiml = generate_twiml_response(reply_message)
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/xml"},
        "body": twiml,
    }


# Expose at module level so the globals() lookup above works for test patching
verify_twilio_signature = _utils.verify_twilio_signature
