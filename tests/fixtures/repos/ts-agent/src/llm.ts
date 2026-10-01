import OpenAI from "openai";

const openai = new OpenAI();

export async function summarize(text: string) {
  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [{ role: "user", content: text }],
  });
  return completion.choices[0].message.content ?? "";
}
