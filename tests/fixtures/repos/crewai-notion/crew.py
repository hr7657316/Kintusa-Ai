from crewai import Agent, Crew, Process, Task

from tools.notion import NotionTool
from tools.slack import post_summary

researcher = Agent(
    role="Researcher",
    goal="Find the most relevant Notion pages for a topic",
    backstory="You know the company wiki inside out.",
    tools=[NotionTool()],
)
task = Task(description="Summarise {topic}", expected_output="A short summary", agent=researcher)
crew = Crew(agents=[researcher], tasks=[task], process=Process.sequential)


def run(topic: str) -> str:
    result = crew.kickoff(inputs={"topic": topic})
    post_summary(str(result))
    return str(result)
