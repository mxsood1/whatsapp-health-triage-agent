"""Local webhook runner for development and testing.

This Flask application mimics the API Gateway + Lambda integration used
in production. It loads environment variables from a `.env` file (via
python‑dotenv) and wires up the same triage logic used in
`lambda_function.py`. You can post form‑encoded requests to
`/webhook` to simulate Twilio webhook calls.

Run with:

```bash
pip install -r requirements.txt
python local_runner.py
```

Then send a POST request to http://localhost:5000/webhook with
parameters `From` and `Body` to see the XML reply.
"""

from __future__ import annotations

import os
from flask import Flask, request, Response
import boto3
from dotenv import load_dotenv

from utils import (
    verify_twilio_signature,
    load_conversation,
    store_conversation,
    upload_transcript,
    classify_message,
    build_response_and_state,
    generate_twiml_response,
)

# Load environment from .env if present
load_dotenv()

app = Flask(__name__)

dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')
sns_client = boto3.client('sns')

table_name = os.getenv('DYNAMODB_TABLE')
bucket_name = os.getenv('S3_BUCKET')
topic_arn = os.getenv('SNS_TOPIC_ARN')
twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN', '')
llm_provider = os.getenv('LLM_PROVIDER', 'openai')

table = dynamodb.Table(table_name) if table_name else None


@app.route('/webhook', methods=['POST'])
def webhook() -> Response:
    # Flask provides request.url (full URL) and form (ImmutableMultiDict)
    full_url = request.url
    params = {k: [v] for k, v in request.form.items()}
    signature = request.headers.get('X-Twilio-Signature', '')

    if not verify_twilio_signature(signature, full_url, params, twilio_auth_token):
        return Response('Invalid signature', status=403)

    user_id = params.get('From', [''])[0]
    message = params.get('Body', [''])[0].strip()
    if not user_id or not message:
        return Response('Missing From or Body', status=400)

    conversation = load_conversation(table, user_id) if table else {}
    triage_result = classify_message(message, conversation.get('history', []), provider=llm_provider)
    reply = build_response_and_state(
        triage_result,
        conversation,
        message,
        sns_client,
        topic_arn,
        user_id,
    )
    if table:
        store_conversation(table, conversation)
    if bucket_name:
        transcript_lines = [f"{msg['timestamp']}: {msg['message']}" for msg in conversation.get('history', [])]
        transcript = "\n".join(transcript_lines)
        upload_transcript(s3_client, bucket_name, user_id, transcript)
    twiml = generate_twiml_response(reply)
    return Response(twiml, mimetype='application/xml')


if __name__ == '__main__':
    app.run(port=5000, debug=True)
