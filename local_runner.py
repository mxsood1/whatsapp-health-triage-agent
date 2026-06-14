"""Local development server — simulates API Gateway + Lambda locally.

Run with:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your values
    python local_runner.py

Then send test requests:
    curl -X POST http://localhost:5000/webhook \\
      -d "From=%2B15005550006&Body=I+have+a+headache"

Twilio signature validation is skipped in local mode. AWS calls use
the credentials configured in your environment (~/.aws/credentials or
environment variables).
"""

from __future__ import annotations

import os

import boto3
from dotenv import load_dotenv
from flask import Flask, Response, request

# Support running from the project root (python local_runner.py)
try:
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
except ImportError:
    from utils import (  # type: ignore
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

load_dotenv()

app = Flask(__name__)

table_name = os.getenv("DYNAMODB_TABLE")
bucket_name = os.getenv("S3_BUCKET")
topic_arn = os.getenv("SNS_TOPIC_ARN")
llm_provider = os.getenv("LLM_PROVIDER", "bedrock")

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
sns_client = boto3.client("sns")
table = dynamodb.Table(table_name) if table_name else None


@app.route("/webhook", methods=["POST"])
def webhook() -> Response:
    # Signature validation intentionally skipped for local development
    params = {k: [v] for k, v in request.form.items()}
    user_id = params.get("From", [""])[0]
    raw_message = params.get("Body", [""])[0].strip()

    if not user_id or not raw_message:
        return Response("Missing From or Body", status=400)

    message = sanitize_input(raw_message)
    conversation = load_conversation(table, user_id) if table else {}

    if is_rate_limited(conversation):
        twiml = generate_twiml_response(
            "You have sent too many messages today. Please try again tomorrow."
        )
        return Response(twiml, mimetype="application/xml")

    increment_message_count(conversation)
    triage_result = classify_message(message, conversation.get("history", []), provider=llm_provider)
    reply = build_response_and_state(
        triage_result, conversation, message, sns_client, topic_arn or "", user_id
    )

    if table:
        store_conversation(table, conversation)
    if bucket_name:
        lines = [
            f"{msg['timestamp']} [{msg.get('role', 'patient')}]: {msg['message']}"
            for msg in conversation.get("history", [])
        ]
        upload_transcript(s3_client, bucket_name, user_id, "\n".join(lines))

    return Response(generate_twiml_response(reply), mimetype="application/xml")


if __name__ == "__main__":
    app.run(port=5000, debug=True)
