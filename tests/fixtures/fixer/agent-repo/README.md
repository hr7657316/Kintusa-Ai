# weather-billing-agent (Kintsua fixer fixture)

A deliberately small agent with two external dependencies:

* `agent/billing.py` calls `stripe.Charge.create(amount=..., currency=..., source=...)`
* `agent/weather.py` calls the MCP tool `get_weather` with `{"city": ...}`

`tests/` pass on the unpatched tree. The recorded breaks in `../breaks/*.json` describe
vendor changes against these call sites and the verdict the validator must reach.
