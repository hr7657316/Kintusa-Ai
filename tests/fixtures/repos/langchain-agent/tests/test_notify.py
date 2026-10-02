from unittest.mock import patch

from agent.notify import notify


def test_notify():
    with patch("agent.notify.slack") as slack:
        slack.chat_postMessage.return_value = {"ts": "1"}
        assert notify("#x", "hi") == "1"
