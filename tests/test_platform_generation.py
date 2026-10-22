from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.providers.llm import factory, model_selection
from app.social.ai import generator, prompts
from app.social.publishers.base import format_caption


BRAND = {
    "brand_name": "Orbit",
    "industry": "Developer tools",
    "tones": ["Calm", "Precise"],
    "target_audience": "Platform engineers",
    "words_to_use": ["predictable"],
    "words_to_avoid": ["revolutionize"],
    "cta_phrases": ["Read the guide"],
    "sentence_length": "short",
    "emoji_usage": "never",
    "primary_language": "fr",
    "tagline": "Reliable releases",
    "system_prompt_override": "Website: https://orbit.example\nPrefer practical explanations.",
}


def response(payload=None, *, raw=None, finish_reason="stop"):
    content = raw if raw is not None else json.dumps(payload)
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, refusal=None), finish_reason=finish_reason,
    )])


@pytest.fixture
def llm(monkeypatch):
    client = Mock()
    client.chat.completions.create.side_effect = AssertionError("Unexpected LLM call")
    monkeypatch.setattr(factory, "get_llm_client", lambda: client)
    monkeypatch.setattr(settings, "bedrock_model_id", "test-text-model")
    monkeypatch.setattr(boto3, "client", Mock(side_effect=AssertionError("No AWS calls allowed")))
    return client.chat.completions.create


def generate(llm, payload=None, **overrides):
    if payload is not None:
        llm.side_effect = [response(payload)]
    kwargs = dict(
        topic="Explain reliable releases.", tone="Professional", platforms=["x"],
        audience=None, cta=None, include_hashtags=True, include_comment=True,
        brand_voice=BRAND, length_hint="short",
    )
    kwargs.update(overrides)
    return generator.generate_platform_content(**kwargs)


def user_prompt(**overrides):
    kwargs = dict(
        topic="Explain reliable releases.", tone="Professional", platform="x",
        audience=None, cta=None, include_hashtags=True, include_comment=False,
        brand_voice=BRAND,
    )
    kwargs.update(overrides)
    return prompts.build_user_prompt(**kwargs)


@pytest.mark.parametrize("platform", prompts.PLATFORM_LIMITS)
def test_structured_brand_survives_custom_instructions(platform):
    prompt = prompts.build_system_prompt(BRAND, platform)
    for value in (
        "Orbit", "Developer tools", "Calm, Precise", "Platform engineers", "predictable",
        "Never use: revolutionize", "Read the guide", "Sentence length: short",
        "Emoji policy: never", "Language: fr", "Reliable releases",
        "https://orbit.example", "Prefer practical explanations.",
        "Supplemental brand instructions", "not a policy override",
        "Do not invent URL paths", "Do not use emojis in any generated field",
    ):
        assert value in prompt
    assert "Website:" not in prompt
    assert "at most one natural CTA" in prompt
    assert "maxima, not quotas" in prompt
    assert "Never invent fake statistics" in prompt


@pytest.mark.parametrize("brand", [None, {}, {"system_prompt_override": "Keep it concrete."}])
def test_default_brand_context_is_always_present(brand):
    prompt = prompts.build_system_prompt(brand, "linkedin")
    assert "Voice tones: Professional" in prompt
    assert "Language: en" in prompt
    assert "Never invent fake statistics" in prompt


def test_empty_website_line_does_not_consume_supplemental_instructions():
    prompt = prompts.build_system_prompt({**BRAND, "system_prompt_override": "Website:\nKeep the original terminology."}, "linkedin")
    assert "<brand_instructions>\nKeep the original terminology.\n</brand_instructions>" in prompt


@pytest.mark.parametrize("length", [None, "short", "medium", "long", " LONG "])
@pytest.mark.parametrize("budget", [244, 280])
def test_x_length_guidance_never_contradicts_hard_budget(length, budget):
    prompt = user_prompt(length_hint=length, caption_budget=budget)
    assert f"hard caption ceiling of {budget}" in prompt
    assert "within 280 characters" in prompt
    assert "220–600" not in prompt
    assert "600–1200" not in prompt
    assert "up to 2 relevant tags" in prompt


@pytest.mark.parametrize("enabled,policy,forbidden", [
    (True, "never", True), (False, "sometimes", True), (True, "sometimes", False),
    (True, " NEVER ", True),
])
def test_brand_never_emoji_takes_precedence(enabled, policy, forbidden):
    prompt = user_prompt(include_emojis=enabled, brand_voice={**BRAND, "emoji_usage": policy})
    assert ("do not use any emojis in any generated field" in prompt) is forbidden


@pytest.mark.parametrize("platform", prompts.PLATFORM_LIMITS)
def test_native_platform_generation_caps_tags_and_uses_canonical_count(llm, platform):
    caption = "Reliable releases start with a repeatable checklist. Read the guide."
    payload = {"caption": caption, "hashtags": [f"Tag{i}" for i in range(40)], "first_comment": "Useful checklist."}
    result = generate(llm, payload, platforms=[platform])[platform]
    assert result["caption"] == caption
    assert len(result["hashtags"]) == prompts.PLATFORM_HASHTAG_LIMITS[platform]
    assert result["firstComment"] == "Useful checklist."
    assert result["characterCount"] == len(format_caption(result["caption"], result["hashtags"]))
    assert result["characterCount"] <= prompts.PLATFORM_LIMITS[platform]
    assert llm.call_count == 1
    system, user = llm.call_args.kwargs["messages"]
    assert prompts.PLATFORM_LABELS[platform] in system["content"]
    assert "Language: fr" in system["content"]
    assert "Emojis: do not use" in user["content"]


@pytest.mark.parametrize("platform", prompts.PLATFORM_LIMITS)
def test_oversized_hashtags_never_destroy_caption(llm, platform):
    caption = "Reliable releases keep teams focused. Read the guide."
    result = generate(llm, {
        "caption": caption, "hashtags": ["Giant" * 3000, "Release"],
    }, platforms=[platform])[platform]
    assert result["caption"] == caption
    assert result["hashtags"] == ["Release"]
    assert result["characterCount"] == len(format_caption(caption, ["Release"]))


@pytest.mark.parametrize("remaining,expected", [(0, []), (4, []), (5, ["ab"])])
def test_x_budget_accounts_for_two_newlines(llm, remaining, expected):
    caption = "A" * (279 - remaining) + "."
    result = generate(llm, {"caption": caption, "hashtags": ["ab"]})["x"]
    assert result["caption"] == caption
    assert result["hashtags"] == expected
    assert result["characterCount"] == len(format_caption(caption, expected)) <= 280


def test_duplicate_case_does_not_consume_hashtag_slots(llm):
    result = generate(llm, {
        "caption": "Reliable releases. #Release",
        "hashtags": ["Release", "release", "RELEASE", "Tools"],
    })["x"]
    assert result["hashtags"] == ["Release", "Tools"]
    assert result["caption"] == "Reliable releases."


def test_disabled_hashtags_and_comment_remain_empty(llm):
    result = generate(llm, {
        "caption": "Reliable releases. #Release", "hashtags": ["Tools"], "first_comment": "Comment.",
    }, include_hashtags=False, include_comment=False)["x"]
    assert result["hashtags"] == []
    assert result["firstComment"] == ""
    assert result["characterCount"] == len(result["caption"])


@pytest.mark.parametrize("limit", [0, 1, 5, 40, 280])
def test_trim_always_obeys_budget(limit):
    caption = generator._trim_caption("A" * 400, limit)
    assert len(caption) <= limit
    assert not caption.endswith("…")


def test_overlength_caption_keeps_complete_sentences_and_drops_tags(llm):
    caption = "Reliable releases protect production. " * 20
    result = generate(llm, {"caption": caption, "hashtags": ["Massive" * 100]})["x"]
    assert result["caption"].endswith("production.")
    assert len(result["caption"]) > 200
    assert result["hashtags"] == []
    assert result["characterCount"] <= 280


@pytest.mark.parametrize("caption", ["Ship safely.", "Reliable releases matter."])
def test_short_request_does_not_trigger_length_polish(llm, caption):
    result = generate(llm, {"caption": caption}, platforms=["linkedin"], content_series_mode=True)
    assert result["linkedin"]["caption"] == caption
    assert llm.call_count == 1


def test_short_request_still_polishes_filler(llm):
    llm.side_effect = [response({"caption": "Unlock your releases."}), response({"caption": "Ship safely."})]
    assert generate(llm)["x"]["caption"] == "Ship safely."
    assert llm.call_count == 2


def test_fidelity_polish_can_improve_short_request(llm):
    topic = "Explain Kubernetes deployment rollbacks with GitOps reconciliation and container health checks for production platform engineers."
    llm.side_effect = [
        response({"caption": "Write with confidence."}),
        response({"caption": "Kubernetes deployment rollbacks rely on GitOps reconciliation and container health checks."}),
    ]
    result = generate(llm, topic=topic)["x"]
    assert "Kubernetes" in result["caption"]
    assert llm.call_count == 2
    assert llm.call_args.kwargs["temperature"] == 0.35
    assert "REVISION REQUIRED" in llm.call_args.kwargs["messages"][1]["content"]


def test_polish_failure_logs_and_retains_best_effort(llm, caplog):
    llm.side_effect = [response({"caption": "Unlock your releases."}), RuntimeError("provider unavailable")]
    with caplog.at_level(logging.WARNING, logger=generator.__name__):
        result = generate(llm)["x"]
    assert result["caption"] == "Unlock your releases."
    assert "polish failed for x" in caplog.text
    assert "provider unavailable" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_empty_length_retry_retains_original_prompts_model_and_temperature(llm):
    caption = "Reliable releases start with a repeatable checklist. Review rollback readiness before deploying."
    llm.side_effect = [response(raw="", finish_reason="length"), response({"caption": caption})]
    with model_selection.selected_models(model_selection.ModelSelection(text="selected-text-model")):
        result = generate(llm, length_hint="long")["x"]
    assert result["caption"] == caption
    assert llm.call_count == 2
    first, retry = [call.kwargs for call in llm.call_args_list[:2]]
    assert retry["messages"][0] == first["messages"][0]
    assert first["messages"][1]["content"] in retry["messages"][1]["content"]
    assert "Write a short" not in retry["messages"][1]["content"]
    assert first["model"] == retry["model"] == "selected-text-model"
    assert first["temperature"] == retry["temperature"] == 0.45
    assert retry["max_completion_tokens"] > first["max_completion_tokens"]


def test_empty_retry_exhaustion_is_502(llm):
    llm.side_effect = [response(raw="", finish_reason="length"), response(raw="", finish_reason="length")]
    with pytest.raises(HTTPException) as exc:
        generate(llm)
    assert exc.value.status_code == 502
    assert llm.call_count == 2


def test_unknown_platforms_are_not_generated(llm):
    with pytest.raises(HTTPException) as exc:
        generate(llm, platforms=["unknown"])
    assert exc.value.status_code == 400
    llm.assert_not_called()


@pytest.mark.parametrize("series,topic,temperature", [
    (False, "Release tips.", 0.45), (True, "Release tips.", 0.5),
    (True, "Explain Kubernetes deployment rollbacks with GitOps reconciliation and container health checks for production platform engineers.", 0.35),
])
def test_generation_temperature_policy_is_preserved(llm, series, topic, temperature):
    generate(llm, {"caption": topic}, topic=topic, content_series_mode=series)
    assert llm.call_args.kwargs["temperature"] == temperature


@pytest.mark.parametrize("format_name", ["carousel", "thread", "poll"])
def test_native_formats_preserve_brand_truthfulness_schema_and_model_override(llm, format_name):
    payloads = {
        "carousel": {"caption": "Reliable releases.", "slides": [
            {"headline": "Plan the release", "body": "Check rollback readiness.", "imagePrompt": "A release checklist"},
            {"headline": "Review the guide", "body": "Read the guide.", "imagePrompt": "A calm workspace"},
        ], "hashtags": [f"Tag{i}" for i in range(20)]},
        "thread": {"tweets": [{"text": "1/ Reliable releases."}, {"text": "2/ Read the guide."}], "hashtags": [f"Tag{i}" for i in range(20)]},
        "poll": {"question": "Which release step needs attention?", "options": ["Planning", "Checks", "Rollbacks", "Review"], "caption": "Vote for your priority.", "hashtags": [f"Tag{i}" for i in range(20)]},
    }
    llm.side_effect = [response(payloads[format_name])]
    kwargs = dict(topic="Reliable releases.", tone="Professional", audience=None, brand_voice=BRAND)
    if format_name != "poll":
        kwargs["cta"] = "Read the guide"
    with model_selection.selected_models(model_selection.ModelSelection(text="chosen-text-model")):
        result = getattr(generator, f"generate_{format_name}_content")(**kwargs)
    call = llm.call_args.kwargs
    assert call["model"] == "chosen-text-model"
    system = call["messages"][0]["content"]
    for value in ("Never invent fake statistics", "Language: fr", "Emoji policy: never", "https://orbit.example", "Prefer practical explanations."):
        assert value in system
    assert "CTA" in system
    assert call["response_format"] == {"type": "json_object"}
    assert llm.call_count == 1
    if format_name == "thread":
        assert len(result["hashtags"]) == prompts.PLATFORM_HASHTAG_LIMITS["x"]
        assert len(result["tweets"]) == 2
        assert all(len(tweet["text"]) <= 270 for tweet in result["tweets"])
        assert len(format_caption(result["tweets"][-1]["text"], result["hashtags"])) <= 280
    else:
        assert len(result["hashtags"]) == prompts.PLATFORM_HASHTAG_LIMITS["linkedin"]
        assert len(result["caption"]) <= 300
        if format_name == "carousel":
            assert result["slides"] == payloads[format_name]["slides"]
        else:
            assert result["options"] == payloads[format_name]["options"]


def test_thread_long_tags_cannot_overflow_last_tweet(llm):
    llm.side_effect = [response({
        "tweets": [{"text": "First sentence. " * 40}, {"text": "Read the guide."}],
        "hashtags": ["Enormous" * 500, "Release"],
    })]
    result = generator.generate_thread_content(topic="Release checklist.", tone="Calm", audience=None, cta=None, brand_voice=BRAND)
    assert all(len(tweet["text"]) <= 270 for tweet in result["tweets"])
    assert result["tweets"][-1]["text"] == "Read the guide."
    assert result["hashtags"] == ["Release"]
    assert len(format_caption(result["tweets"][-1]["text"], result["hashtags"])) <= 280


def test_poll_returns_four_options_with_native_limits(llm):
    llm.side_effect = [response({"question": "Release readiness? " * 20, "options": ["A" * 60], "caption": "Vote here. " * 80})]
    result = generator.generate_poll_content(topic="Release checklist.", tone="Calm", audience=None, brand_voice=BRAND)
    assert len(result["question"]) <= 140
    assert len(result["caption"]) <= 300
    assert len(result["options"]) == 4
    assert all(len(option) <= 25 for option in result["options"])


def test_network_failure_is_not_retried_as_optional_parameter_error(llm):
    llm.side_effect = RuntimeError("network timeout")
    with pytest.raises(HTTPException) as exc:
        generate(llm)
    assert exc.value.status_code == 502
    assert llm.call_count == 1


def test_optional_parameter_fallback_keeps_brand_and_model(llm):
    llm.side_effect = [RuntimeError("response_format not supported"), response({"caption": "Ship safely."})]
    generate(llm)
    first, second = [call.kwargs for call in llm.call_args_list]
    assert first["messages"] == second["messages"]
    assert first["model"] == second["model"]
    assert first["temperature"] == second["temperature"]
    assert "reasoning_effort" not in second


@pytest.mark.parametrize("format_name,payload", [
    ("carousel", {"caption": "Release checklist.", "slides": [{"headline": "Plan"}, {"headline": "Review"}]}),
    ("thread", {"tweets": [{"text": "Plan carefully."}, {"text": "Review readiness."}]}),
    ("poll", {"question": "Which step?", "options": ["Plan", "Review", "Check", "Deploy"]}),
])
def test_format_empty_retry_keeps_brand_and_original_schema(llm, format_name, payload):
    llm.side_effect = [response(raw="", finish_reason="length"), response(payload)]
    kwargs = dict(topic="Release checklist.", tone="Calm", audience=None, brand_voice=BRAND)
    if format_name != "poll":
        kwargs["cta"] = None
    getattr(generator, f"generate_{format_name}_content")(**kwargs)
    original, retry = [call.kwargs for call in llm.call_args_list]
    assert retry["messages"][0] == original["messages"][0]
    assert original["messages"][1]["content"] in retry["messages"][1]["content"]
    assert "Language: fr" in retry["messages"][0]["content"]


def test_adopted_polish_uses_same_caption_first_budgeting(llm):
    caption = "Reliable releases start with a repeatable checklist. Review rollback readiness before deploying."
    llm.side_effect = [
        response({"caption": "Unlock your releases."}),
        response({"caption": caption, "hashtags": ["Huge" * 500, "Release"], "first_comment": "Review the checklist."}),
    ]
    result = generate(llm)["x"]
    assert result["caption"] == caption
    assert result["hashtags"] == ["Release"]
    assert result["firstComment"] == "Review the checklist."
    assert result["characterCount"] == len(format_caption(caption, ["Release"])) <= 280


def test_worse_fidelity_revision_keeps_original(llm):
    topic = "Explain Kubernetes deployment rollbacks with GitOps reconciliation and container health checks for production platform engineers."
    llm.side_effect = [
        response({"caption": "Kubernetes protects releases."}),
        response({"caption": "Write with confidence."}),
    ]
    assert generate(llm, topic=topic)["x"]["caption"] == "Kubernetes protects releases."


def test_multi_platform_best_effort_is_preserved(llm, caplog):
    def complete(**kwargs):
        if "Write for Facebook" in kwargs["messages"][1]["content"]:
            raise RuntimeError("Facebook generation unavailable")
        return response({"caption": "Ship safely."})

    llm.side_effect = complete
    with caplog.at_level(logging.WARNING, logger=generator.__name__):
        result = generate(llm, platforms=["x", "facebook", "unknown"])
    assert set(result) == {"x"}
    assert "Partial social generate success" in caplog.text
    assert llm.call_count == 2