import os
from unittest import mock

import boto3
import pytest
from moto import mock_dynamodb, mock_s3, mock_sns

from src import lambda_function


@mock_dynamodb
@mock_s3
@mock_sns
def test_lambda_handler_low_flow():
    """Test lambda_handler end‑to‑end for a low urgency message."""
    # Set environment variables
    os.environ['DYNAMODB_TABLE'] = 'test-table'
    os.environ['S3_BUCKET'] = 'test-bucket'
    os.environ['SNS_TOPIC_ARN'] = 'arn:aws:sns:us-east-1:123456789012:test-topic'
    os.environ['TWILIO_AUTH_TOKEN'] = 'test-token'
    os.environ['LLM_PROVIDER'] = 'unknown'  # force fallback classifier

    # Create mocked AWS resources
    region = 'us-east-1'
    dynamodb = boto3.resource('dynamodb', region_name=region)
    dynamodb.create_table(
        TableName='test-table',
        KeySchema=[{'AttributeName': 'user_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'user_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    s3 = boto3.client('s3', region_name=region)
    s3.create_bucket(Bucket='test-bucket')
    sns = boto3.client('sns', region_name=region)
    sns.create_topic(Name='test-topic')

    # Build a sample event; Twilio would URL‑encode + characters as %2B
    body = 'From=%2B1234567890&Body=I%20have%20a%20mild%20headache'
    event = {
        'body': body,
        'headers': {
            'X-Twilio-Signature': 'dummy-signature',
        },
        'requestContext': {
            'domainName': 'example.com',
            'http': {'path': '/webhook'},
        },
    }

    # Patch verify_twilio_signature to always return True
    with mock.patch('src.lambda_function.verify_twilio_signature', return_value=True):
        # Reload module variables to pick up environment changes
        import importlib

        importlib.reload(lambda_function)
        response = lambda_function.lambda_handler(event, None)

    assert response['statusCode'] == 200
    assert 'application/xml' in response['headers']['Content-Type']
    assert 'rest, stay hydrated' in response['body'].lower() or 'self-care' in response['body'].lower()
