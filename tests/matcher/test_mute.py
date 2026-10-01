"""``.kintsua.yml`` mute patterns (pure, no database)."""

from __future__ import annotations

from kintsua.matcher.mute import clean_patterns, is_muted, mute_candidates


def test_vendor_and_path_glob():
    pats = ["stripe:/v1/legacy/*"]
    assert is_muted(pats, "stripe", "POST /v1/legacy/charges")
    assert is_muted(pats, "stripe", "get /v1/legacy/charges/{id}")
    assert not is_muted(pats, "stripe", "POST /v1/charges")
    assert not is_muted(pats, "slack", "POST /v1/legacy/charges")  # other vendor
    assert is_muted(["stripe:POST /v1/charges"], "stripe", "POST /v1/charges")


def test_tool_glob_matches_bare_and_prefixed_names():
    pats = ["tool:get_*"]
    assert is_muted(pats, "weather-mcp", "get_weather")
    assert is_muted(pats, None, "tool:get_forecast")
    assert not is_muted(pats, "weather-mcp", "set_location")
    assert is_muted(["get_*"], "weather-mcp", "get_weather")


def test_anonymous_path_glob_crosses_vendors():
    pats = ["*/beta/*"]
    assert is_muted(pats, "stripe", "POST /v1/beta/charges")
    assert is_muted(pats, None, "GET /beta/x")
    assert not is_muted(pats, "stripe", "POST /v1/charges")


def test_sdk_symbol_is_muted_through_its_http_equivalent_and_by_symbol():
    assert is_muted(["stripe:/v1/charges"], "stripe", "stripe.Charge.create")
    assert is_muted(["stripe:stripe.Charge.*"], "stripe", "stripe.Charge.create")
    assert is_muted(["sdk:stripe.Charge.create"], "stripe", "stripe.Charge.create")
    assert not is_muted(["stripe:/v1/customers/*"], "stripe", "stripe.Charge.create")


def test_bare_vendor_mutes_everything_from_it():
    assert is_muted(["slack"], "slack", "slack_sdk.WebClient.chat_postMessage")
    assert is_muted(["slack:*"], "slack", "POST /chat.postMessage")
    assert not is_muted(["slack"], "stripe", "POST /v1/charges")


def test_empty_or_garbage_patterns_never_mute():
    assert not is_muted([], "stripe", "POST /v1/charges")
    assert not is_muted(None, "stripe", "POST /v1/charges")
    assert not is_muted(["", "   ", 42], "stripe", "POST /v1/charges")  # type: ignore[list-item]
    assert not is_muted(["stripe:*"], "stripe", "")
    assert clean_patterns(["", " a ", None]) == ["a"]  # type: ignore[list-item]


def test_candidates_include_every_spelling():
    cands = mute_candidates("stripe", "post /v1/charges/ch_1Nxyz123456789/")
    assert "stripe:POST /v1/charges/{id}" in cands
    assert "/v1/charges/{id}" in cands
    assert "stripe" in cands
    assert "stripe:post /v1/charges/ch_1Nxyz123456789/" in cands
