import axios from "axios";

const GITHUB = "https://api.github.com";
const notion = axios.create({
  baseURL: "https://api.notion.com/v1",
  headers: { "Notion-Version": "2022-06-28" },
});

export async function openIssue(owner: string, repo: string, title: string, body: string) {
  const res = await fetch(`${GITHUB}/repos/${owner}/${repo}/issues`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.GITHUB_TOKEN}` },
    body: JSON.stringify({ title, body }),
  });
  return (await res.json()).number;
}

export async function searchNotion(query: string) {
  const res = await notion.post("/search", { query, page_size: 5 });
  return res.data.results;
}

export async function getPage(pageId: string) {
  const res = await notion.get(`/pages/${pageId}`);
  return res.data;
}

export async function weather(lat: number, lon: number) {
  const res = await axios.get(`https://api.weather.gov/points/${lat},${lon}`);
  return res.data;
}
