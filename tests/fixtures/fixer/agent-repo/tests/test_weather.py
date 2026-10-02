from agent.weather import get_forecast


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"summary": "sunny"}


def test_forecast_calls_the_weather_tool():
    client = FakeClient()
    assert get_forecast(client, "Lisbon") == "Lisbon: sunny"
    (name, args) = client.calls[0]
    assert name == "get_weather"
    assert args["city"] == "Lisbon"
