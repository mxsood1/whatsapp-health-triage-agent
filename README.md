# WhatsApp Healthcare Triage Agent

> **AI-powered patient triage via WhatsApp — serverless, event-driven, and fully AWS-native.**

[![CI](https://github.com/mxsood1/whatsapp-health-triage-agent/actions/workflows/python.yml/badge.svg)](https://github.com/mxsood1/whatsapp-health-triage-agent/actions/workflows/python.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
![AWS SAM](https://img.shields.io/badge/IaC-AWS%20SAM-orange)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What it does

Patients send a WhatsApp message describing their symptoms. The agent uses **AWS Bedrock (Claude 3 Haiku)** to analyse the message, classify urgency as **LOW / MEDIUM / HIGH**, and reply instantly with appropriate guidance — all without a single server to manage.

- **LOW** → Self-care tips and advice to monitor symptoms
- **MEDIUM** → Prompts the patient to book an appointment
- **HIGH** → Emergency instructions + immediate SNS alert to healthcare staff

---

## Architecture

```
WhatsApp User
     │  (message)
     ▼
  Twilio
     │  POST (signed webhook)
     ▼
API Gateway HTTP API
     │
     ▼
AWS Lambda (Python 3.11 · Graviton2 · arm64)
     │
     ├─► DynamoDB          — conversation state + 90-day TTL
     ├─► AWS Bedrock       — Claude 3 Haiku for triage classification
     ├─► Amazon S3         — encrypted transcript archive
     ├─► Amazon SNS        — HIGH-urgency staff alert
     └─► CloudWatch        — structured logs + dashboard + alarm
     │
     ▼
TwiML response → Twilio → WhatsApp reply
```

---

## Tech Stack

| Layer | Service | Detail |
|---|---|---|
| Messaging | Twilio WhatsApp | Webhook with HMAC-SHA1 signature validation |
| API | Amazon API Gateway | HTTP API (serverless, no idle cost) |
| Compute | AWS Lambda | Python 3.11, Graviton2 arm64, 256 MB |
| AI / LLM | AWS Bedrock | Claude 3 Haiku (default) or OpenAI GPT-3.5 |
| State | Amazon DynamoDB | On-demand capacity, encrypted at rest, 90-day TTL |
| Archive | Amazon S3 | AES-256 encryption, public access blocked, 1-yr lifecycle |
| Alerts | Amazon SNS | Email subscription for HIGH-urgency cases |
| Monitoring | Amazon CloudWatch | Structured JSON logs, dashboard, error alarm |
| IaC | AWS SAM | Single `sam deploy` command to provision everything |
| CI/CD | GitHub Actions | Runs on every push with coverage reporting |

---

## Key Features

- **Fully serverless** — zero servers to manage, scales to zero when idle
- **AWS-native LLM** — Bedrock keeps data within your AWS account; no external API dependency
- **Conversation memory** — DynamoDB persists full chat history; the LLM sees prior messages for better context
- **Rate limiting** — 50 messages per user per day, enforced server-side
- **Security hardened** — Twilio signature validation, S3 public access blocked, TLS-only bucket policy, DynamoDB encryption at rest, XML output escaped to prevent injection
- **Data minimisation** — phone numbers masked in logs; conversation data auto-expires after 90 days via DynamoDB TTL; S3 objects expire after 1 year
- **Operational visibility** — CloudWatch dashboard with Lambda metrics (invocations, errors, p50/p99 duration), DynamoDB capacity, and an error-rate alarm
- **Keyword fallback** — if Bedrock is unavailable the system still triages using keyword matching so patients always get a response

---

## Prerequisites

- AWS account (free tier works for low volume)
- [AWS CLI](https://aws.amazon.com/cli/) configured (`aws configure`)
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Twilio account](https://www.twilio.com/) with a WhatsApp-enabled number
- Python 3.11+
- Bedrock model access enabled for `anthropic.claude-3-haiku-20240307-v1:0` in your region

> **Enabling Bedrock:** AWS Console → Amazon Bedrock → Model access → Request access for *Claude 3 Haiku*. Approval is usually instant.

---

## Deploy to AWS

```bash
# 1. Clone the repository
git clone https://github.com/mxsood1/whatsapp-health-triage-agent.git
cd whatsapp-health-triage-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Build and deploy (follow the prompts)
sam build
sam deploy --guided
```

The guided deploy will ask for:

| Parameter | Description |
|---|---|
| `LLMProvider` | `bedrock` (recommended) or `openai` |
| `TwilioAuthToken` | From Twilio Console → Account → API keys |
| `AlertEmailAddress` | (Optional) Email for HIGH-urgency alerts |
| `OpenAIApiKey` | Only needed if `LLMProvider=openai` |

After deploy, copy the **WebhookUrl** from the Outputs and paste it into your Twilio WhatsApp sandbox/number as the "A message comes in" webhook URL.

---

## Local Development

```bash
# Copy and fill in env vars
cp .env.example .env

# Run the Flask development server (port 5000)
python local_runner.py

# Test with curl (Twilio signature validation is skipped locally)
curl -X POST http://localhost:5000/webhook \
  -d "From=%2B15005550006&Body=I+have+chest+pain"
```

---

## Running Tests

```bash
# All tests with coverage report
pytest --cov=src --cov-report=term-missing -v

# Quick run
pytest -q
```

Tests use `moto` to mock all AWS services — no real AWS account required.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | Yes | `bedrock` or `openai` |
| `DYNAMODB_TABLE` | Yes | DynamoDB table name (set by SAM) |
| `S3_BUCKET` | Yes | S3 bucket name (set by SAM) |
| `SNS_TOPIC_ARN` | Yes | SNS topic ARN (set by SAM) |
| `TWILIO_AUTH_TOKEN` | Yes | Webhook signature validation |
| `OPENAI_API_KEY` | No | Only when `LLM_PROVIDER=openai` |
| `AWS_REGION` | No | Defaults to Lambda execution region |

---

## Estimated AWS Cost

For a small healthcare practice (~500 conversations/month):

| Service | Estimate |
|---|---|
| Lambda (500 invocations × 2s avg) | ~$0.00 (free tier) |
| API Gateway (500 requests) | ~$0.00 (free tier) |
| DynamoDB (on-demand, ~500 writes) | ~$0.01 |
| S3 (1 MB transcripts) | ~$0.00 |
| Bedrock Claude 3 Haiku | ~$0.10–$0.50 |
| SNS (a few alerts) | ~$0.00 |
| **Total** | **< $1/month** |

---

## How Triage Classification Works

1. The patient's latest message plus the last 10 conversation turns are sent to the LLM as context
2. Claude 3 Haiku returns structured JSON: `{symptoms, duration, age, red_flags, urgency}`
3. If the LLM call fails, a keyword-based fallback classifier handles the message
4. Urgency is mapped to a response branch (LOW / MEDIUM / HIGH)

**Example LLM output:**
```json
{
  "symptoms": ["chest tightness", "shortness of breath"],
  "duration": "30 minutes",
  "age": "52",
  "red_flags": ["chest tightness", "shortness of breath"],
  "urgency": "HIGH"
}
```

---

## Security Considerations

- **Twilio signature validation** on every request prevents spoofed webhooks
- **Phone numbers are masked** in all logs (only last 4 digits retained)
- **S3 bucket** blocks all public access and enforces TLS
- **DynamoDB** uses AWS-managed encryption at rest
- **TwiML output** is XML-escaped to prevent injection attacks
- **Rate limiting** (50 messages/user/day) prevents cost abuse
- **Bedrock** keeps patient data within your AWS account — no data sent to third parties when using the default provider
- This system is **not a diagnostic tool** and must not replace professional medical advice

---

## Project Structure

```
whatsapp-health-triage-agent/
├── src/
│   ├── lambda_function.py   # Lambda handler — webhook validation & orchestration
│   └── utils.py             # Core logic — triage, DynamoDB, S3, SNS, TwiML
├── tests/
│   ├── test_lambda_function.py   # Integration tests (7 scenarios)
│   └── test_utils.py             # Unit tests (20+ cases)
├── template.yaml            # AWS SAM — all infrastructure as code
├── local_runner.py          # Flask dev server for local testing
├── requirements.txt
└── .env.example
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

> **Disclaimer:** This application is a triage tool only. It does not provide medical diagnoses, treatment recommendations, or clinical advice. Always direct patients to qualified healthcare professionals.
