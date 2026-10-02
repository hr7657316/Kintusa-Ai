import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


def _client() -> WebClient:
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def post_summary(text: str, channel: str = "#research") -> None:
    client = _client()
    try:
        client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as exc:
        client.files_upload_v2(
            channel=channel, content=text, title="summary", initial_comment=str(exc)
        )


def who_am_i(client: WebClient) -> str:
    return client.auth_test()["user_id"]
