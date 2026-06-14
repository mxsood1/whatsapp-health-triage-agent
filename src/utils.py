"""Utility functions for the WhatsApp Healthcare Triage Agent.

Core business logic: Twilio signature validation, DynamoDB state management,
LLM classification (Bedrock or OpenAI), TwiML generation, and SNS alerting.
All AWS clients are passed in as parameters to keep functions testable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

try:
    import openai  # type: ignore
except Exception:
    openai = None

try:
    from twilio.request_validator import RequestValidator  # type: ignore
except Exception:
    RequestValidator = None

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 1600   # WhatsApp character limit
MAX_DAILY_MESSAGES = 50     # Abuse prevention
CONVERSATION_TTL_DAYS = 90  # Auto-expire data after 90 days (GDPR/HIPAA-adjacent)

BEDROCK_MODEL_ID = "anthropic.claude-3-haiku-20240307-v1:0"
OPENAI_MODEL_ID = "gpt-3.5-turbo-1106"


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def sanitize_input(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> str:
    """Strip whitespace and enforce a maximum length on user input."""
    return text.strip()[:max_length]


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def is_rate_limited(conversation: Dict[str, Any]) -> bool:
    """Return True if the user has exceeded MAX_DAILY_MESSAGES today."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if conversation.get('count_date') != today:
        conversation['count_date'] = today
        conversation['daily_count'] = 0
        return False
    return int(conversation.get('daily_count', 0)) >= MAX_DAILY_MESSAGES


def increment_message_count(conversation: Dict[str, Any]) -> None:
    """Increment the rolling daily message counter, resetting on a new day."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if conversation.get('count_date') != today:
        conversation['count_date'] = today
        conversation['daily_count'] = 1
    else:
        conversation['daily_count'] = int(conversation.get('daily_count', 0)) + 1


# ---------------------------------------------------------------------------
# Twilio signature validation
# ---------------------------------------------------------------------------

def verify_twilio_signature(
    signature: str,
    url: str,
    params: Dict[str, List[str]],
    auth_token: str,
) -> bool:
    """Validate the X-Twilio-Signature header to confirm the request is genuine.

    Uses the official Twilio library when available; falls back to a manual
    HMAC-SHA1 implementation that is constant-time to prevent timing attacks.
    """
    if RequestValidator is not None:
        validator = RequestValidator(auth_token)
        flat_params = {k: v[0] if isinstance(v, list) else v for k, v in params.items()}
        return bool(validator.validate(url, flat_params, signature))

    sorted_params = sorted(
        (k, v[0] if isinstance(v, list) else v) for k, v in params.items()
    )
    data = url + "".join(k + v for k, v in sorted_params)
    digest = hmac.new(
        auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, signature)


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------

def load_conversation(table, user_id: str) -> Dict[str, Any]:
    """Load conversation state from DynamoDB, returning defaults for new users."""
    try:
        response = table.get_item(Key={"user_id": user_id})
        item = response.get("Item")
        if item:
            return item
    except Exception as exc:
        logger.error(
            json.dumps({"event": "dynamodb_load_error", "user": _mask(user_id), "error": str(exc)})
        )
    return _default_conversation(user_id)


def store_conversation(table, conversation: Dict[str, Any]) -> None:
    """Persist conversation state to DynamoDB with a 90-day TTL for auto-expiry."""
    now = datetime.now(timezone.utc)
    conversation["updated_at"] = now.isoformat()
    # DynamoDB TTL expects an epoch-seconds integer
    conversation["ttl"] = int(now.timestamp()) + (CONVERSATION_TTL_DAYS * 86_400)
    try:
        table.put_item(Item=conversation)
    except Exception as exc:
        logger.error(json.dumps({"event": "dynamodb_store_error", "error": str(exc)}))


def _default_conversation(user_id: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "user_id": user_id,
        "history": [],
        "last_intent": None,
        "triage_level": None,
        "daily_count": 0,
        "count_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# S3 transcript archival
# ---------------------------------------------------------------------------

def upload_transcript(s3_client, bucket: str, user_id: str, transcript: str) -> None:
    """Archive the conversation transcript to S3 under a masked user prefix."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{_mask(user_id)}/transcript_{timestamp}.txt"
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=transcript.encode("utf-8"),
            ContentType="text/plain",
            ServerSideEncryption="AES256",
        )
    except Exception as exc:
        logger.error(json.dumps({"event": "s3_upload_error", "error": str(exc)}))


# ---------------------------------------------------------------------------
# SNS alerting
# ---------------------------------------------------------------------------

def send_alert(sns_client, topic_arn: str, subject: str, message: str) -> None:
    """Publish a high-urgency alert to the SNS topic."""
    try:
        sns_client.publish(TopicArn=topic_arn, Subject=subject, Message=message)
    except Exception as exc:
        # Log the error but don't crash — the patient still needs a reply
        logger.error(json.dumps({"event": "sns_alert_error", "error": str(exc)}))


# ---------------------------------------------------------------------------
# TwiML generation
# ---------------------------------------------------------------------------

def generate_twiml_response(message: str) -> str:
    """Wrap a reply message in a TwiML XML envelope, escaping special characters."""
    escaped = saxutils.escape(message)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"    <Message>{escaped}</Message>\n"
        "</Response>"
    )


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------

def classify_message(
    message: str,
    history: List[Dict[str, Any]],
    provider: str = "bedrock",
    openai_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify a patient message and return a structured triage result.

    Tries the configured LLM provider first; falls back to keyword-based
    classification if the LLM call fails or is unavailable.

    Returns a dict with keys: symptoms, duration, age, red_flags, urgency.
    urgency is one of: LOW | MEDIUM | HIGH.
    """
    resolved_provider = (provider or os.getenv("LLM_PROVIDER", "bedrock")).lower()

    system_prompt = (
        "You are a medical triage assistant. Analyze the patient's message "
        "and any prior conversation context provided. Extract: a list of symptoms, "
        "how long symptoms have lasted (duration), the patient's age if mentioned, "
        "and any red-flag emergency symptoms. "
        "Classify urgency as HIGH (call emergency services immediately), "
        "MEDIUM (see a doctor within 24 hours), or LOW (safe for self-care). "
        "IMPORTANT: You are NOT a diagnostic tool — only classify urgency. "
        "Respond ONLY with valid JSON matching this schema exactly: "
        '{"symptoms": ["string"], "duration": "string", "age": "string", '
        '"red_flags": ["string"], "urgency": "LOW|MEDIUM|HIGH"}'
    )

    context = _build_context(history)
    user_content = (
        f"Conversation history:\n{context}\n\nLatest message: {message}"
        if context
        else message
    )

    if resolved_provider == "bedrock":
        return _classify_bedrock(user_content, system_prompt, message)
    if resolved_provider == "openai":
        return _classify_openai(user_content, system_prompt, message, openai_api_key)

    logger.warning(json.dumps({"event": "unknown_provider", "provider": resolved_provider}))
    return _fallback_classifier(message)


def _classify_bedrock(
    user_content: str, system_prompt: str, raw_message: str
) -> Dict[str, Any]:
    """Invoke AWS Bedrock (Claude 3 Haiku) for triage classification."""
    try:
        region = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
        client = boto3.client("bedrock-runtime", region_name=region)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        })
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"].strip()

        # Strip markdown code fences if the model wrapped the JSON
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()

        result = json.loads(text)
        result.setdefault("urgency", "LOW")
        result["urgency"] = result["urgency"].upper()
        logger.info(json.dumps({"event": "bedrock_classification", "urgency": result["urgency"]}))
        return result
    except Exception as exc:
        logger.error(json.dumps({"event": "bedrock_error", "error": str(exc)}))
        return _fallback_classifier(raw_message)


def _classify_openai(
    user_content: str,
    system_prompt: str,
    raw_message: str,
    openai_api_key: Optional[str],
) -> Dict[str, Any]:
    """Call the OpenAI Chat Completions API for triage classification."""
    if openai is None:
        logger.warning(json.dumps({"event": "openai_not_installed"}))
        return _fallback_classifier(raw_message)

    api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning(json.dumps({"event": "openai_no_api_key"}))
        return _fallback_classifier(raw_message)

    try:
        client = openai.OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL_ID,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=512,
        )
        text = completion.choices[0].message.content.strip()
        result = json.loads(text)
        result.setdefault("urgency", "LOW")
        result["urgency"] = result["urgency"].upper()
        logger.info(json.dumps({"event": "openai_classification", "urgency": result["urgency"]}))
        return result
    except Exception as exc:
        logger.error(json.dumps({"event": "openai_error", "error": str(exc)}))
        return _fallback_classifier(raw_message)


def _fallback_classifier(message: str) -> Dict[str, Any]:
    """Keyword-based classifier used when no LLM provider is available."""
    text = message.lower()
    high_keywords = [
        "chest pain", "shortness of breath", "difficulty breathing",
        "fainting", "unconscious", "heart attack", "stroke",
        "not breathing", "severe bleeding", "overdose", "seizure",
        "anaphylaxis", "allergic reaction",
    ]
    medium_keywords = [
        "fever", "vomiting", "vomit", "infection", "severe pain",
        "broken bone", "deep cut", "high temperature", "dehydrated",
        "painful urination", "rash spreading",
    ]

    urgency = "LOW"
    red_flags: List[str] = []

    for kw in high_keywords:
        if kw in text:
            urgency = "HIGH"
            red_flags.append(kw)
            break
    else:
        for kw in medium_keywords:
            if kw in text:
                urgency = "MEDIUM"
                break

    return {
        "symptoms": [message],
        "duration": "",
        "age": "",
        "red_flags": red_flags,
        "urgency": urgency,
    }


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def build_response_and_state(
    triage_result: Dict[str, Any],
    conversation: Dict[str, Any],
    message: str,
    sns_client,
    topic_arn: str,
    user_id: str,
) -> str:
    """Append the latest exchange to the conversation and return the reply text."""
    urgency = triage_result.get("urgency", "LOW").upper()
    now_iso = datetime.now(timezone.utc).isoformat()

    history = conversation.get("history", [])
    history.append({"timestamp": now_iso, "role": "patient", "message": message})
    conversation["history"] = history
    conversation["triage_level"] = urgency
    conversation["last_intent"] = "triage"

    if urgency == "HIGH":
        subject = f"HIGH urgency triage alert — patient {_mask(user_id)}"
        body = (
            f"Patient {_mask(user_id)} requires immediate attention.\n\n"
            f"Symptoms: {', '.join(triage_result.get('symptoms', []))}\n"
            f"Red flags: {', '.join(triage_result.get('red_flags', []))}\n"
            f"Duration: {triage_result.get('duration', 'unknown')}\n\n"
            "Please contact the patient immediately.\n\n"
            f"Full triage data:\n{json.dumps(triage_result, indent=2)}"
        )
        send_alert(sns_client, topic_arn, subject, body)
        reply = (
            "Based on what you've described, this sounds like it could be a medical emergency.\n\n"
            "Please call emergency services (911 / 999 / 112) or go to your nearest "
            "emergency room right away.\n\n"
            "Do not drive yourself — call an ambulance or ask someone to take you."
        )

    elif urgency == "MEDIUM":
        reply = (
            "Thank you for sharing that. Your symptoms suggest you should see a doctor "
            "soon — ideally within the next 24 hours.\n\n"
            "To schedule an appointment, please reply with:\n"
            "- Your full name\n"
            "- A preferred date and time\n\n"
            "If your condition worsens before then, please call emergency services."
        )

    else:
        reply = (
            "Your symptoms sound mild. Here are some self-care tips:\n\n"
            "- Rest and stay well hydrated\n"
            "- Monitor your symptoms over the next 24-48 hours\n"
            "- Take over-the-counter medication as appropriate\n\n"
            "If your symptoms worsen or new ones appear, please consult a healthcare professional.\n\n"
            "Reminder: This service does not provide medical diagnoses."
        )

    history.append({"timestamp": now_iso, "role": "agent", "message": reply})
    conversation["history"] = history
    return reply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_context(history: List[Dict[str, Any]]) -> str:
    """Format the last 10 conversation turns for inclusion in the LLM prompt."""
    if not history:
        return ""
    lines = []
    for entry in history[-10:]:
        role = entry.get("role", "patient").capitalize()
        msg = entry.get("message", "")
        ts = entry.get("timestamp", "")
        lines.append(f"[{ts}] {role}: {msg}")
    return "\n".join(lines)


def _mask(user_id: str) -> str:
    """Mask a phone number for logs — keep only the last 4 digits."""
    clean = user_id.lstrip("+").lstrip("whatsapp:")
    return f"***{clean[-4:]}" if len(clean) > 4 else "****"
