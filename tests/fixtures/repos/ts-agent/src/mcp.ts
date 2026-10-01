import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export async function searchDocs(query: string) {
  const client = new Client({ name: "support-agent", version: "1.0.0" });
  const transport = new StreamableHTTPClientTransport(new URL("https://mcp.deepwiki.com/mcp"));
  await client.connect(transport);
  const result = await client.callTool({
    name: "ask_question",
    arguments: { repoName: "acme/docs", question: query },
  });
  await client.close();
  return result.content;
}
