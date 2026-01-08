"""Utility functions for the WhatsApp Healthcare Triage Agent.

This module contains helper functions for verifying Twilio signatures,
loading and saving conversation state in DynamoDB, interacting with
the LLM provider and constructing TwiML responses.

The functions are written to be easy to test and to avoid coupling the
core business logic to AWS services. For example, all boto3 resources
are passed in as parameters rather than instantiated globally (except
where specified).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

try:
    import openai  # type: ignore
except Exception:
    openai = None  # OpenAI may not be installed when running tests

try:
    from twilio.request_validator import RequestValidator  # type: ignore
except Exception:
    RequestValidator = None  # Twilio may not be installed when running tests


def verify_twilio_signature(signature: str, url: str, params: Dict[str, List[str]], auth_token: str) -> bool:
    """Validate the Twilio request signature.

    Twilio sends a SHA‑1 HMAC signature in the `X‑Twilio‑Signature` header. This
    function recomputes the signature using the full URL and form parameters
    and compares it securely. See Twilio’s docs for details:
    https://www.twilio.com/docs/usage/security#validating-requests

    Args:
        signature: The `X‑Twilio‑Signature` header value.
        url: The full URL of the webhook endpoint (including query string).
        params: A dictionary of POST parameters. Values should be lists of
            strings as returned by `urllib.parse.parse_qs`.
        auth_token: Your Twilio auth token.

    Returns:
        True if the signature is valid, False otherwise.
    """
    # Use twilio library if available for convenience
    if RequestValidator is not None:
        validator = RequestValidator(auth_token)
        # Flatten parameter values to strings for validator
        flat_params = {k: v[0] if isinstance(v, list) else v for k, v in params.items()}
        return bool(validator.validate(url, flat_params, signature))

    # If twilio library is unavailable, compute the HMAC manually
    # Build the data string by concatenating the URL and sorted parameters
    sorted_params = sorted((k, v[0] if isinstance(v, list) else v) for k, v in params.items())
    data = url + ''.join(k + v for k, v in sorted_params)
    digest = hmac.new(auth_token.encode('utf-8'), data.encode('utf-8'), hashlib.sha1).digest()
    computed_signature = base64.b64encode(digest).decode('utf-8')
    # Compare using hmac.compare_digest to avoid timing attacks
    return hmac.compare_digest(computed_signature, signature)


def load_conversation(table, user_id: str) -> Dict[str, Any]:
    """Load the conversation state for a user from DynamoDB.

    Args:
        table: A DynamoDB Table resource.
        user_id: The unique identifier (e.g. phone number) of the user.

    Returns:
        A dictionary containing the conversation state. If no state exists
        for the user, a default structure is returned.
    """
    try:
        response = table.get_item(Key={'user_id': user_id})
    except Exception:
        response = {}
    item = response.get('Item')
    if not item:
        return {
            'user_id': user_id,
            'history': [],
            'last_intent': None,
            'triage_level': None,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
    return item


def store_conversation(table, conversation: Dict[str, Any]) -> None:
    """Store the conversation state in DynamoDB.

    Args:
        table: A DynamoDB Table resource.
        conversation: The conversation dict to persist.
    """
    conversation['updated_at'] = datetime.now(timezone.utc).isoformat()
    table.put_item(Item=conversation)


def upload_transcript(s3_client, bucket: str, user_id: str, transcript: str) -> None:
    """Upload the conversation transcript to S3 as a text file.

    Each transcript is stored under a prefix per user with a timestamp to
    avoid overwriting previous transcripts.

    Args:
        s3_client: A Boto3 S3 client.
        bucket: Name of the bucket to upload transcripts to.
        user_id: The user identifier.
        transcript: The full conversation transcript as a string.
    """
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    key = f"{user_id}/transcript_{timestamp}.txt"
    s3_client.put_object(Bucket=bucket, Key=key, Body=transcript.encode('utf-8'))


def send_alert(sns_client, topic_arn: str, subject: str, message: str) -> None:
    """Publish a message to an SNS topic.

    Args:
        sns_client: A Boto3 SNS client.
        topic_arn: ARN of the SNS topic.
        subject: Subject of the email/SMS.
        message: Body of the notification.
    """
    sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)


def generate_twiml_response(message: str) -> str:
    """Generate a TwiML XML response with the given message.

    Args:
        message: The text to include in the WhatsApp reply.

    Returns:
        A string containing a minimal TwiML XML document.
    """
    # We avoid depending on the twilio library for generating simple responses
    return f"""<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n    <Message>{message}</Message>\n</Response>"""


def classify_message(
    message: str,
    history: List[Dict[str, str]],
    provider: str = 'openai',
    openai_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify a user message into a structured triage result.

    This function calls the configured LLM provider to extract relevant
    fields and classify urgency. The expected output schema is:

    ```json
    {
      "symptoms": ["string", ...],
      "duration": "string",  // description of how long symptoms persisted
      "age": "string",        // optional age if provided
      "red_flags": ["string", ...],
      "urgency": "LOW"|"MEDIUM"|"HIGH"
    }
    ```

    If the provider is set to `openai`, the function uses the ChatCompletion
    API with a system prompt instructing the model to return a JSON
    object matching the schema above. If the call fails or the provider
    is unknown, a simple keyword‑based fallback classifier is used.

    Args:
        message: The user's latest message.
        history: A list of previous messages in the conversation.
        provider: Which LLM provider to use (`openai` or `bedrock`).
        openai_api_key: API key for OpenAI. If None, the environment
            variable `OPENAI_API_KEY` is used.

    Returns:
        A dict with extracted fields and an `urgency` key.
    """
    # Simple fallback classifier (keyword based). This is used if OpenAI
    # integration is unavailable or disabled. It looks for keywords like
    # "chest pain" or "difficulty breathing" to mark high urgency.
    def fallback_classifier(msg: str) -> Dict[str, Any]:
        text = msg.lower()
        high_keywords = ['chest pain', 'shortness of breath', 'fainting', 'unconscious']
        medium_keywords = ['fever', 'vomit', 'infection', 'severe pain']
        urgency = 'LOW'
        for kw in high_keywords:
            if kw in text:
                urgency = 'HIGH'
                break
        else:
            for kw in medium_keywords:
                if kw in text:
                    urgency = 'MEDIUM'
                    break
        return {
            'symptoms': [msg],
            'duration': '',
            'age': '',
            'red_flags': [],
            'urgency': urgency,
        }

    provider = provider or os.getenv('LLM_PROVIDER', 'openai')
    if provider == 'openai' and openai is not None:
        api_key = openai_api_key or os.getenv('OPENAI_API_KEY')
        if not api_key:
            return fallback_classifier(message)
        openai.api_key = api_key
        system_prompt = (
            "You are a medical triage assistant. For the incoming patient's message, "
            "extract a list of symptoms, the duration of symptoms, the patient's age if provided, "
            "and any red‑flag symptoms (things that suggest an emergency). Then classify the overall "
            "urgency of the situation into one of the categories: LOW, MEDIUM, HIGH. "
            "Respond only with a JSON object containing the keys: symptoms (array of strings), duration (string), "
            "age (string), red_flags (array of strings), and urgency (string)."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]
        try:
            completion = openai.ChatCompletion.create(model="gpt-3.5-turbo-1106", messages=messages)
            # The model should return a JSON string in the assistant's message
            response_text = completion.choices[0].message['content'].strip()
            result = json.loads(response_text)
            # basic sanity checks
            if 'urgency' not in result:
                result['urgency'] = 'LOW'
            return result
        except Exception:
            return fallback_classifier(message)
    else:
        # TODO: Implement AWS Bedrock integration if provider == 'bedrock'.
        return fallback_classifier(message)


def build_response_and_state(
    triage_result: Dict[str, Any],
    conversation: Dict[str, Any],
    message: str,
    sns_client,
    topic_arn: str,
    user_id: str,
) -> str:
    """Update conversation state based on triage result and build a reply message.

    Args:
        triage_result: The output from `classify_message` including `urgency`.
        conversation: Current conversation state dictionary.
        message: The latest user message.
        sns_client: Boto3 SNS client for notifications.
        topic_arn: SNS topic ARN.
        user_id: The user identifier (phone number).

    Returns:
        A string containing the WhatsApp reply message.
    """
    urgency = triage_result.get('urgency', 'LOW').upper()
    # Append the latest message to history
    history = conversation.get('history', [])
    history.append({'timestamp': int(time.time()), 'message': message})
    conversation['history'] = history
    conversation['triage_level'] = urgency
    conversation['last_intent'] = 'triage'
    reply: str
    if urgency == 'HIGH':
        # Construct alert
        alert_subject = f"High urgency triage alert for user {user_id}"
        alert_message = (
            f"User {user_id} has reported symptoms requiring immediate attention.\n"
            f"Extracted fields: {json.dumps(triage_result, indent=2)}\n\n"
            "Please contact the patient as soon as possible."
        )
        send_alert(sns_client, topic_arn, alert_subject, alert_message)
        reply = (
            "Based on the symptoms you described, it may be an emergency. "
            "Please call your local emergency number or visit the nearest emergency room immediately."
        )
    elif urgency == 'MEDIUM':
        reply = (
            "Thanks for providing more details. It sounds like your situation is not urgent, "
            "but we would like to schedule an appointment. Please reply with your name and a preferred "
            "day/time for a call or visit."
        )
    else:  # LOW
        reply = (
            "It appears your symptoms are mild. Here are some general self‑care tips: rest, stay hydrated and monitor your symptoms. "
            "If they worsen or new symptoms appear, please contact a healthcare professional."
        )
    return reply
