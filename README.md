# WhatsApp Healthcare Triage Agent

This repository contains a serverless **WhatsApp Healthcare Triage Agent** that
collects patient symptoms via WhatsApp, classifies the urgency of the
conversation using a large language model, stores conversation context in
AWS and routes the conversation to the appropriate path (self‑care guidance,
scheduling follow‑up or immediate escalation).

## Architecture

```mermaid
graph LR
    A[WhatsApp (Twilio)] -->|Webhook| B((API Gateway))
    B --> C{Lambda Handler}
    C -->|Persist state| D[DynamoDB Table]
    C -->|Log transcript| E[S3 Bucket]
    C -->|Classify| F[LLM (OpenAI/Bedrock)]
    C -->|Metrics| G[CloudWatch]
    C -->|Notify high urgency| H[SNS Topic]
```

The WhatsApp channel is provided by **Twilio**. Incoming messages are sent to
an Amazon API Gateway HTTP API backed by an AWS Lambda function written in
Python. The handler performs the following steps:

1. **Validate the request** – Checks the Twilio signature to ensure the
   request originates from Twilio.
2. **Load conversation state** – Reads the patient’s conversation history and
   metadata from a DynamoDB table.
3. **Classify the message** – Sends the latest message and conversation
   context to an LLM (OpenAI or AWS Bedrock) to extract symptoms, duration,
   red‑flags and classify urgency (`LOW`, `MEDIUM`, `HIGH`).
4. **Route the conversation** – Depending on the urgency level, responds
   appropriately and, for high urgency, triggers a notification via
   Amazon SNS. Medium urgency conversations collect scheduling details while
   low urgency conversations provide safe self‑care guidance.
5. **Store the state** – Writes the updated conversation back to DynamoDB and
   uploads a transcript to S3. All operations emit logs and metrics to
   CloudWatch.

The infrastructure is defined using **AWS Serverless Application Model (SAM)**.

## Features

* **WhatsApp Webhook:** An API Gateway endpoint that Twilio can POST to.
* **Twilio Signature Validation:** Ensures all requests are genuine.
* **Conversation Persistence:** Stores conversation history and metadata
  (phone number, last intent, triage level, timestamps) in DynamoDB.
* **LLM‑powered Triage:** Uses OpenAI’s API by default (easily swapped
  for AWS Bedrock) to extract relevant medical information and classify
  urgency.
* **Routing Logic:** Responds differently based on the urgency:
  - **HIGH:** Instructs the patient to seek emergency care immediately and
    sends a notification email via SNS to healthcare staff.
  - **MEDIUM:** Asks follow‑up questions for scheduling (name, preferred
    appointment time) and stores them.
  - **LOW:** Provides general, safe self‑care advice and reminds users to
    contact a professional if symptoms worsen.
* **Logging & Monitoring:** Writes conversation transcripts to S3,
  publishes metrics and structured logs to CloudWatch and includes a
  basic CloudWatch alarm example.
* **Local development:** A simple Flask app simulates the webhook locally and
  includes unit tests using `pytest` and `moto`.
* **GitHub Actions:** A workflow runs tests on every push.

## Getting Started

### Prerequisites

* [Python 3.11](https://www.python.org/downloads/) with `pip`.
* An AWS account with permissions to deploy serverless resources.
* Twilio account with a WhatsApp number and webhook configured.
* OpenAI API key (or AWS Bedrock access) for LLM classification.

### Setup

1. **Clone the repository** and install dependencies:

   ```bash
   git clone https://github.com/your‑account/whatsapp-health-triage-agent
   cd whatsapp-health-triage-agent
   pip install -r requirements.txt
   ```

2. **Copy `.env.example` to `.env`** and fill in the required environment
   variables. These are read by the Lambda function and local runner.

3. **Configure Twilio webhook:** Point your Twilio WhatsApp number’s
   webhook URL at the API Gateway endpoint you will deploy.

4. **Deploy with SAM:**

   ```bash
   sam build
   sam deploy --guided
   ```

   The guided deploy will prompt for parameters such as the stack name,
   AWS region, S3 bucket for code uploads, and values for the DynamoDB
   table, S3 bucket and SNS topic. Once deployed, note the API endpoint
   and update your Twilio configuration accordingly.

### Local Development

The local runner in `local_runner.py` provides a Flask server to
simulate the Twilio webhook locally. It reads from `.env` and uses
mocked AWS resources.

```bash
export FLASK_APP=local_runner.py
flask run --port 5000
```

You can send test POST requests to `http://localhost:5000/webhook` with
form‑encoded data mimicking Twilio’s payload. See `tests/test_lambda_function.py`
for examples.

### Testing

Tests are written with `pytest` and use `moto` to mock AWS services. To run
the test suite locally:

```bash
pytest -q
```

### Environmental Variables

The application expects the following environment variables (see
`.env.example` for details):

| Variable              | Description                                         |
|-----------------------|-----------------------------------------------------|
| `OPENAI_API_KEY`      | API key for OpenAI (if using OpenAI for LLM).       |
| `LLM_PROVIDER`        | `openai` or `bedrock`. Default is `openai`.         |
| `DYNAMODB_TABLE`      | Name of the DynamoDB table for state.              |
| `S3_BUCKET`           | Name of the S3 bucket for storing transcripts.     |
| `SNS_TOPIC_ARN`       | ARN of the SNS topic for high‑urgency alerts.       |
| `TWILIO_AUTH_TOKEN`   | Your Twilio auth token used for signature validation.|

## Security Notes

* This application is **not** a diagnostic tool. It is designed
  exclusively to triage messages and give high‑level guidance. Always
  instruct users to seek professional medical advice.
* All personally identifiable and medical information should be handled
  with care. Only the minimum necessary data is stored and logs are
  redacted where appropriate.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for
 details.
