import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def notify(channel: str, text: str) -> str:
    try:
        resp = slack.chat_postMessage(channel=channel, text=text, unfurl_links=False)
    except SlackApiError as e:
        raise RuntimeError(e.response["error"])
    return resp["ts"]


def list_channels():
    return slack.conversations_list(limit=200, types="public_channel")["channels"]
