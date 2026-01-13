"""AWS Lambda handler for the WhatsApp Healthcare Triage Agent.


This module contains the entry point used by AWS Lambda. It integrates
Twilio webhook requests with the underlying triage logic defined in
`utils.py`. The handler expects requests from API Gateway (HTTP API)
forwarding Twilio webhook POSTs. It validates the request signature,
loads and updates conversation state from DynamoDB, calls the LLM to
classify the message and returns a TwiML XML response.
"""

from __future__ import annotations

import json
import logging
import os

import sys

from typing import Any, Dict, List

import boto3
from urllib.parse import parse_qs

from src.utils import (
    verify_twilio_signature,
    load_conversation,
    store_conversation,
    upload_transcript,
    classify_message,
    build_response_and_state,
    generate_twiml_response,
)


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients outside of handler for reuse between invocations
dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')

table_name = os.environ.get('DYNAMODB_TABLE')
bucket_name = os.environ.get('S3_BUCKET')
topic_arn = os.environ.get('SNS_TOPIC_ARN')
twilio_auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
llm_provider = os.environ.get('LLM_PROVIDER', 'openai')

if not table_name or not bucket_name or not topic_arn or not twilio_auth_token:
    logger.warning(
        "Environment variables DYNAMODB_TABLE, S3_BUCKET, SNS_TOPIC_ARN and TWILIO_AUTH_TOKEN must be set."
    )

table = dynamodb.Table(table_name) if table_name else None


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda entry point for processing WhatsApp webhook events.

    Args:
        event: The incoming event from API Gateway. For HTTP APIs the
            body is provided as a string and headers are provided in
            `event['headers']`. The requestContext contains the domain
            name and path.
        context: The Lambda context object (unused).

    Returns:
        A dictionary with `statusCode`, `headers` and `body` keys.
    """
    logger.info("Received event: %s", json.dumps(event))

    # API Gateway HTTP API passes the body as a raw string
    body = event.get('body', '') or ''
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    signature = headers.get('x-twilio-signature', '')

    # Construct the full URL used by Twilio to compute the signature.
    # event['requestContext']['domainName'] looks like abcd1234.execute-api.region.amazonaws.com
    # event['requestContext']['http']['path'] contains the resource path (e.g. /webhook).
    request_context = event.get('requestContext', {})
    domain = request_context.get('domainName', '')
    path = request_context.get('http', {}).get('path', '')
    protocol = 'https'  # API Gateway endpoints are HTTPS
    full_url = f"{protocol}://{domain}{path}"

    # Parse the body as form data (Twilio posts application/x-www-form-urlencoded)
    params: Dict[str, List[str]] = parse_qs(body)

    # Validate signature
    verify_fn = getattr(sys.modules[__name__], "verify_twilio_signature", verify_twilio_signature)

    if not verify_fn(signature, full_url, params, twilio_auth_token or ''):
        logger.warning("Invalid Twilio signature")
        return {
            'statusCode': 403,
            'body': 'Invalid signature',
        }

    # Extract fields from the POST body
    user_id = params.get('From', [''])[0]
    message = params.get('Body', [''])[0].strip()
    if not user_id or not message:
        return {
            'statusCode': 400,
            'body': 'Missing From or Body parameter',
        }

    # Load conversation state
    conversation = load_conversation(table, user_id) if table else {}

    # Call LLM classifier
    triage_result = classify_message(message, conversation.get('history', []), provider=llm_provider)

    # Build reply and update conversation state
    reply_message = build_response_and_state(
        triage_result,
        conversation,
        message,
        sns_client,
        topic_arn,
        user_id,
    )

    # Persist conversation and transcript
    if table:
        store_conversation(table, conversation)
    if bucket_name:
        transcript_lines = [f"{msg['timestamp']}: {msg['message']}" for msg in conversation.get('history', [])]
        transcript = "\n".join(transcript_lines)
        upload_transcript(s3_client, bucket_name, user_id, transcript)

    # Generate TwiML and return response
    twiml = generate_twiml_response(reply_message)
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/xml',
        },
        'body': twiml,
    }
