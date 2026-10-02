import anthropic

client = anthropic.Anthropic()


def ask(prompt: str) -> str:
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def ask_with_mcp(prompt: str) -> str:
    msg = client.beta.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        mcp_servers=[{"type": "url", "url": "https://mcp.deepwiki.com/mcp", "name": "deepwiki"}],
        betas=["mcp-client-2025-04-04"],
    )
    return msg.content[0].text
