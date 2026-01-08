import os
from unittest import mock

import boto3
import pytest
from moto import mock_sns

from src.utils import classify_message, build_response_and_state


def test_classify_message_fallback_low():
    """Fallback classifier should mark unknown symptoms as LOW urgency."""
    result = classify_message("I have a mild headache.", history=[], provider="unknown")
    assert result['urgency'] == 'LOW'


def test_classify_message_fallback_medium():
    """Fallback classifier should classify medium keywords correctly."""
    result = classify_message("My child has a high fever and is vomiting.", history=[], provider="unknown")
    assert result['urgency'] == 'MEDIUM'


def test_classify_message_fallback_high():
    """Fallback classifier should detect high urgency phrases."""
    result = classify_message("Sudden chest pain and difficulty breathing", history=[], provider="unknown")
    assert result['urgency'] == 'HIGH'


@mock_sns
def test_build_response_and_state_high():
    """High urgency should trigger SNS alert and emergency advice."""
    sns_client = boto3.client('sns', region_name='us-east-1')
    topic_arn = sns_client.create_topic(Name='alerts')['TopicArn']
    triage_result = {
        'urgency': 'HIGH',
        'symptoms': ['chest pain'],
        'duration': '',
        'age': '',
        'red_flags': [],
    }
    conversation = {'history': []}
    message = 'I have severe chest pain.'
    user_id = '+123'
    reply = build_response_and_state(
        triage_result,
        conversation,
        message,
        sns_client,
        topic_arn,
        user_id,
    )
    # The reply should instruct the user to seek emergency care
    assert 'emergency' in reply.lower()
    # The conversation state should be updated
    assert conversation['triage_level'] == 'HIGH'
    assert len(conversation['history']) == 1
