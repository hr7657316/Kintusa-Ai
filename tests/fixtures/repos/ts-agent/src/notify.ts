import { WebClient } from "@slack/web-api";

const slack = new WebClient(process.env.SLACK_TOKEN);

export async function notify(channel: string, text: string) {
  const res = await slack.chat.postMessage({ channel, text, unfurl_links: false });
  return res.ts;
}
