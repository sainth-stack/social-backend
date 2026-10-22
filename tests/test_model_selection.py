from __future__ import annotations

import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth.deps import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.providers.llm import bedrock_image, factory, model_selection as selection
from app.social import router as social_router
from app.workspaces.deps import require_workspace_access

GPT = "openai.gpt-oss-120b-1:0"
GPT_SMALL = "openai.gpt-oss-20b-1:0"
SD = "stability.sd3-5-large-v1:0"
ULTRA = "stability.stable-image-ultra-v1:1"
CORE = "stability.stable-image-core-v1:1"
NOVA = "amazon.nova-canvas-v1:0"
SONNET = "us.anthropic.claude-sonnet-4-6"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
ROOT = "/workspaces/workspace/social"


def _denied():
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "ListFoundationModels")


def _foundation(model_id, modality="TEXT", lifecycle="ACTIVE", inference="ON_DEMAND"):
    return {"modelId": model_id, "outputModalities": [modality], "modelLifecycle": {"status": lifecycle}, "inferenceTypesSupported": [inference]}


def _profile(model_id, status="ACTIVE", kind="SYSTEM_DEFINED"):
    return {"inferenceProfileId": model_id, "status": status, "type": kind}


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    selection._cache.clear()
    factory._get_llm_client.cache_clear()
    monkeypatch.setattr(settings, "bedrock_model_id", "configured-default")
    monkeypatch.setattr(settings, "bedrock_image_model_id", SD)
    monkeypatch.setattr(settings, "bedrock_image_region", None)
    monkeypatch.setattr(settings, "bedrock_region", "us-east-1")
    monkeypatch.setattr(settings, "bedrock_aws_access_key_id", "test-key")
    monkeypatch.setattr(settings, "bedrock_aws_secret_access_key", "test-secret")
    control = Mock()
    control.list_foundation_models.side_effect = _denied()
    control.list_inference_profiles.side_effect = _denied()

    def client(service, **kwargs):
        assert service == "bedrock"
        return control

    monkeypatch.setattr(boto3, "client", client)
    yield control
    selection._cache.clear()
    factory._get_llm_client.cache_clear()
    assert selection.current_models() == selection.ModelSelection()


@pytest.fixture
def model_client():
    app = FastAPI()
    app.include_router(social_router.router)
    app.dependency_overrides[require_workspace_access] = lambda: {"id": "workspace"}
    app.dependency_overrides[get_current_user] = lambda: {"id": "user"}
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[social_router._invalidate_dashboard_after_write] = lambda: None
    with TestClient(app) as client:
        yield client


def test_discovery_denied_is_honest_and_curated():
    catalog = selection.get_model_catalog()
    assert len(catalog["textModels"]) == 8
    assert {m["id"] for m in catalog["imageModels"]} == {SD, ULTRA, CORE}
    assert {m["provider"] for m in catalog["textModels"]} == {"Anthropic", "OpenAI", "DeepSeek", "Amazon"}
    assert all(m["availability"] == "unknown" for m in catalog["textModels"] + catalog["imageModels"])
    assert all(not m["invokeAccessVerified"] for m in catalog["textModels"] + catalog["imageModels"])
    assert all(d["foundationModels"] == d["inferenceProfiles"] == "access_denied" for d in catalog["discovery"])
    assert catalog["defaultTextModelId"] == "configured-default"
    assert catalog["defaultImageModelId"] == SD
    assert catalog["invokeAccessVerified"] is False
    assert all("gpt-5" not in m["id"] for m in catalog["textModels"])
    assert {m["id"] for m in catalog["imageModels"] if m["supportsImageVariation"]} == {SD, ULTRA}
    assert all(m["region"] == "us-west-2" for m in catalog["imageModels"])


def test_discovery_filters_models_and_uses_active_profiles(isolated_models):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": [
        _foundation(GPT), _foundation(GPT_SMALL, lifecycle="RETIRED"),
        _foundation("deepseek.v3-v1:0", inference="PROVISIONED"),
        _foundation(SD, "IMAGE"), _foundation(NOVA, "IMAGE", lifecycle="RETIRED"),
        _foundation("untrusted.model-v1:0"),
    ]}
    isolated_models.list_inference_profiles.side_effect = None
    isolated_models.list_inference_profiles.return_value = {"inferenceProfileSummaries": [
        _profile(SONNET), _profile(HAIKU, status="INACTIVE"),
        _profile("us.deepseek.r1-v1:0", kind="APPLICATION"),
        _profile("us.untrusted.model-v1:0"),
    ]}
    catalog = selection.get_model_catalog()
    assert {m["id"] for m in catalog["textModels"]} == {GPT, SONNET}
    assert [m["id"] for m in catalog["imageModels"]] == [SD]
    assert all(m["availability"] == "catalogued" for m in catalog["textModels"])
    assert not catalog["invokeAccessVerified"]


def test_latest_available_sonnet_fallback(isolated_models):
    old = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": []}
    isolated_models.list_inference_profiles.side_effect = None
    isolated_models.list_inference_profiles.return_value = {"inferenceProfileSummaries": [_profile(old)]}
    assert [m["id"] for m in selection.get_model_catalog()["textModels"]] == [old]


def test_image_catalog_excludes_retired_nova_even_if_discovered_active(isolated_models):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": [
        _foundation(SD, "IMAGE"), _foundation(ULTRA, "IMAGE"),
        _foundation(CORE, "IMAGE"), _foundation(NOVA, "IMAGE"),
    ]}
    catalog = selection.get_model_catalog()
    assert [m["id"] for m in catalog["imageModels"]] == [SD, ULTRA, CORE]
    assert all(m["availability"] == "catalogued" for m in catalog["imageModels"])
    assert all(not m["invokeAccessVerified"] for m in catalog["imageModels"])
    for model in (SD, ULTRA, CORE):
        assert selection.validate_model_selection(None, model).image == model


@pytest.mark.parametrize("summary", [
    _foundation(ULTRA, "IMAGE", lifecycle="RETIRED"),
    _foundation(ULTRA, "TEXT"),
    _foundation(ULTRA, "IMAGE", inference="PROVISIONED"),
])
def test_unusable_image_models_are_not_unknown_fallbacks(isolated_models, summary):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": [summary]}
    assert selection.get_model_catalog()["imageModels"] == []
    with pytest.raises(HTTPException) as exc:
        selection.validate_model_selection(None, ULTRA)
    assert exc.value.status_code == 422


def test_profile_discovery_is_paginated(isolated_models):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": []}
    isolated_models.list_inference_profiles.side_effect = lambda **kw: (
        {"inferenceProfileSummaries": [_profile(SONNET)]} if kw.get("nextToken") else {"nextToken": "page2"}
    )
    assert [m["id"] for m in selection.get_model_catalog()["textModels"]] == [SONNET]
    assert any(call.kwargs == {"nextToken": "page2"} for call in isolated_models.list_inference_profiles.call_args_list)


def test_partial_discovery_preserves_known_models(isolated_models):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": [_foundation(GPT)]}
    catalog = selection.get_model_catalog()
    models = {m["id"]: m for m in catalog["textModels"]}
    assert models[GPT]["availability"] == "catalogued"
    assert models[SONNET]["availability"] == "unknown"
    assert GPT_SMALL not in models
    assert catalog["imageModels"] == []


def test_discovery_cache_expires_and_separates_credentials(isolated_models, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(selection.time, "monotonic", lambda: clock[0])
    selection.get_model_catalog()
    selection.get_model_catalog()
    assert isolated_models.list_foundation_models.call_count == 2
    clock[0] += selection.CATALOG_TTL_SECONDS + 1
    selection.get_model_catalog()
    assert isolated_models.list_foundation_models.call_count == 4
    monkeypatch.setattr(settings, "bedrock_aws_secret_access_key", "changed-secret")
    selection.get_model_catalog()
    assert isolated_models.list_foundation_models.call_count == 6


def test_missing_credentials_never_contacts_aws(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_aws_access_key_id", None)
    monkeypatch.setattr(settings, "bedrock_aws_secret_access_key", None)
    monkeypatch.setattr(settings, "aws_access_key_id", None)
    monkeypatch.setattr(settings, "aws_secret_access_key", None)
    client = Mock(side_effect=AssertionError("No AWS requests allowed"))
    monkeypatch.setattr(boto3, "client", client)
    catalog = selection.get_model_catalog()
    assert all(d["foundationModels"] == "unavailable" for d in catalog["discovery"])
    client.assert_not_called()


def test_control_client_uses_configured_region_and_credentials(monkeypatch):
    mocked = Mock()
    monkeypatch.setattr(boto3, "client", mocked)
    selection._control_client("us-west-2")
    args, kwargs = mocked.call_args
    assert args == ("bedrock",)
    assert kwargs["region_name"] == "us-west-2"
    assert kwargs["aws_access_key_id"] == "test-key"
    assert kwargs["aws_secret_access_key"] == "test-secret"


@pytest.mark.parametrize("text,image", [("gpt-5", None), ("arbitrary.model", None), ("", None), (None, GPT), (SD, None), (None, NOVA), (None, "stability.stable-image-ultra-v1:0"), (None, "stability.stable-image-core-v1:0")])
def test_invalid_selection_rejected_before_discovery(text, image, monkeypatch):
    catalog = Mock(side_effect=AssertionError("Invalid models must not trigger discovery"))
    monkeypatch.setattr(selection, "get_model_catalog", catalog)
    with pytest.raises(HTTPException) as exc:
        selection.validate_model_selection(text, image)
    assert exc.value.status_code == 422
    catalog.assert_not_called()


def test_region_absent_model_is_rejected(isolated_models):
    isolated_models.list_foundation_models.side_effect = None
    isolated_models.list_foundation_models.return_value = {"modelSummaries": []}
    isolated_models.list_inference_profiles.side_effect = None
    isolated_models.list_inference_profiles.return_value = {"inferenceProfileSummaries": []}
    with pytest.raises(HTTPException) as exc:
        selection.validate_model_selection(GPT, None)
    assert exc.value.status_code == 422


def test_no_headers_preserves_defaults_without_discovery(monkeypatch):
    monkeypatch.setattr(selection, "get_model_catalog", Mock(side_effect=AssertionError("No discovery needed")))
    models = selection.validate_model_selection(None, None)
    with selection.selected_models(models):
        assert factory.get_llm_model() == "configured-default"
        assert bedrock_image._resolved_image_model_id() == SD


def test_nested_selection_and_exception_cleanup():
    models = selection.validate_model_selection(GPT, ULTRA)
    with selection.selected_models(models):
        assert factory.get_llm_model() == GPT
        assert bedrock_image._resolved_image_model_id() == ULTRA
        with pytest.raises(RuntimeError):
            with selection.selected_models(selection.ModelSelection(text=GPT_SMALL, image=SD)):
                assert factory.get_llm_model() == GPT_SMALL
                raise RuntimeError("failure")
        assert factory.get_llm_model() == GPT
    assert factory.get_llm_model() == settings.bedrock_model_id == "configured-default"
    assert settings.bedrock_image_model_id == SD


@pytest.mark.parametrize("default", ["", "configured-default"])
def test_selected_text_id_forwarded_to_converse_and_cache_isolated(monkeypatch, default):
    monkeypatch.setattr(settings, "bedrock_model_id", default)
    runtime = Mock()
    runtime.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
    mocked = Mock(return_value=runtime)
    monkeypatch.setattr(factory, "get_bedrock_runtime_client", mocked)
    with selection.selected_models(selection.ModelSelection(text=GPT)):
        client = factory.get_llm_client()
        assert factory.llm_configured()
        assert factory.get_llm_client() is client
        client.chat.completions.create(model=factory.get_llm_model(), messages=[{"role": "user", "content": "test"}])
        assert runtime.converse.call_args.kwargs["modelId"] == GPT
    if default:
        other = factory.get_llm_client()
        assert other is not client
        assert factory.get_llm_client() is other
    else:
        assert not factory.llm_configured()
        with pytest.raises(RuntimeError, match="BEDROCK_MODEL_ID"):
            factory.get_llm_client()
    assert mocked.call_args.kwargs == {"region_name": "us-east-1"}
    assert settings.bedrock_model_id == default


@pytest.mark.parametrize("model,expected_region", [(SD, "us-west-2"), (ULTRA, "us-west-2"), (CORE, "us-west-2")])
@pytest.mark.parametrize("explicit_region", [None, "eu-west-1"])
@pytest.mark.parametrize("variation", [False, True])
def test_image_id_and_selected_region_forwarded(monkeypatch, model, expected_region, explicit_region, variation):
    monkeypatch.setattr(settings, "bedrock_image_region", explicit_region)
    monkeypatch.setattr(settings, "bedrock_image_model_id", ULTRA if model == SD else SD)
    runtime = Mock()
    runtime.invoke_model.return_value = {"body": io.BytesIO(json.dumps({"images": [base64.b64encode(b"image").decode()]}).encode())}
    mocked = Mock(return_value=runtime)
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", mocked)
    with selection.selected_models(selection.ModelSelection(image=model)):
        if variation and model == CORE:
            with pytest.raises(RuntimeError, match="text-to-image only"):
                bedrock_image.generate_image_variation(prompt="test", source_image_bytes=b"source")
            mocked.assert_not_called()
            return
        if variation:
            image = bedrock_image.generate_image_variation(prompt="test", source_image_bytes=b"source")
        else:
            image = bedrock_image.generate_text_to_image(prompt="test")
    assert image == b"image"
    assert mocked.call_args.kwargs["region_name"] == (explicit_region or expected_region)
    assert runtime.invoke_model.call_args.kwargs["modelId"] == model
    body = json.loads(runtime.invoke_model.call_args.kwargs["body"])
    assert body["prompt"] == "test"
    assert body["output_format"] == "png"
    assert "taskType" not in body
    if variation:
        assert body["image"] == base64.b64encode(b"source").decode()
        assert body["strength"] == 0.65
        assert "aspect_ratio" not in body
        if model == SD:
            assert body["mode"] == "image-to-image"
        else:
            assert "mode" not in body
    else:
        assert body["aspect_ratio"] == "1:1"
        assert body["negative_prompt"]
        assert "image" not in body
        if model == CORE:
            assert "mode" not in body
        else:
            assert body["mode"] == "text-to-image"


@pytest.mark.parametrize("model", [NOVA, "amazon.titan-image-generator-v2:0", "unknown.image-v1:0", "stability.unknown-v1:0", "stability.stable-image-ultra-v1:0", "stability.stable-image-core-v1:0"])
@pytest.mark.parametrize("variation", [False, True])
def test_unsupported_configured_image_model_never_invokes_aws(monkeypatch, model, variation):
    monkeypatch.setattr(settings, "bedrock_image_model_id", model)
    runtime = Mock(side_effect=AssertionError("Unsupported models must not invoke AWS"))
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", runtime)
    assert not bedrock_image.image_generation_configured()
    with pytest.raises(RuntimeError, match="retired|Unsupported"):
        if variation:
            bedrock_image.generate_image_variation(prompt="test", source_image_bytes=b"source")
        else:
            bedrock_image.generate_text_to_image(prompt="test")
    runtime.assert_not_called()


def test_supported_selection_overrides_retired_image_default(monkeypatch):
    monkeypatch.setattr(settings, "bedrock_image_model_id", NOVA)
    with selection.selected_models(selection.ModelSelection(image=ULTRA)):
        assert bedrock_image.image_generation_configured()
        assert bedrock_image._resolved_image_model_id() == ULTRA


@pytest.mark.parametrize("model", [SD, ULTRA, CORE])
def test_image_text_payload_limits_prompt_and_maps_aspect_ratio(monkeypatch, model):
    invoke = Mock(return_value=[base64.b64encode(b"image").decode()])
    monkeypatch.setattr(bedrock_image, "_invoke_raw", invoke)
    with selection.selected_models(selection.ModelSelection(image=model)):
        assert bedrock_image.generate_text_to_image(prompt="x" * 10001, size="1920x1080") == b"image"
    body = invoke.call_args.args[0]
    assert body["prompt"] == "x" * 10000
    assert body["aspect_ratio"] == "16:9"
    assert set(body) == {"prompt", "aspect_ratio", "output_format", "negative_prompt"} | ({"mode"} if model != CORE else set())


@pytest.mark.parametrize("payload,reason", [
    ({"finish_reasons": ["Filter reason: prompt"]}, "Filter reason: prompt"),
    ({"images": [None], "finish_reasons": ["Filter reason: output image"]}, "Filter reason: output image"),
    ({"images": [None]}, "no images returned"),
    ({"images": [""]}, "no images returned"),
    ({"images": []}, "no images returned"),
    ({"error": "generation failed"}, "generation failed"),
])
def test_image_response_errors_are_reported_without_retry(monkeypatch, payload, reason):
    runtime = Mock()
    runtime.invoke_model.return_value = {"body": io.BytesIO(json.dumps(payload).encode())}
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", Mock(return_value=runtime))
    with pytest.raises(RuntimeError, match=reason):
        bedrock_image.generate_text_to_image(prompt="test")
    runtime.invoke_model.assert_called_once()


def test_stability_response_with_null_finish_reason_decodes(monkeypatch):
    runtime = Mock()
    runtime.invoke_model.return_value = {"body": io.BytesIO(json.dumps({"images": [base64.b64encode(b"image").decode()], "finish_reasons": [None]}).encode())}
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", Mock(return_value=runtime))
    assert bedrock_image.generate_text_to_image(prompt="test") == b"image"


@pytest.mark.parametrize("code,message,expected", [
    ("ResourceNotFoundException", "Model not found", "us-west-2"),
    ("AccessDeniedException", "Not authorized", "IAM denied"),
    ("ValidationException", "Model reached the end of its life", "retired"),
])
def test_image_invocation_errors_do_not_recommend_or_retry_nova(monkeypatch, code, message, expected):
    runtime = Mock()
    runtime.invoke_model.side_effect = ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")
    monkeypatch.setattr(bedrock_image, "get_bedrock_runtime_client", Mock(return_value=runtime))
    with selection.selected_models(selection.ModelSelection(image=ULTRA)):
        with pytest.raises(RuntimeError, match=expected) as exc:
            bedrock_image.generate_text_to_image(prompt="test")
    assert "nova" not in str(exc.value).lower()
    assert "Titan" not in str(exc.value)
    runtime.invoke_model.assert_called_once()


def test_executor_receives_captured_text_model(monkeypatch):
    from app.social.ai import generator

    monkeypatch.setattr(factory, "get_bedrock_runtime_client", lambda **kw: Mock())
    seen = []

    def generate(**kwargs):
        seen.append(kwargs["model"])
        return kwargs["platform_key"], {"caption": "test", "hashtags": [], "characterCount": 4}

    monkeypatch.setattr(generator, "_generate_one_platform", generate)
    with selection.selected_models(selection.ModelSelection(text=GPT)):
        result = generator.generate_platform_content(topic="test", tone="Professional", platforms=["facebook", "instagram"], audience=None, cta=None, include_hashtags=False, include_comment=False, brand_voice=None)
    assert set(result) == {"facebook", "instagram"}
    assert seen == [GPT, GPT]


@pytest.mark.parametrize("endpoint,payload,method", [
    ("generate", {"topic": "test topic", "platforms": ["facebook"]}, "generate_post"),
    ("generate-image", {"topic": "test topic"}, "generate_image"),
    ("brand-voice/test", {}, "test_brand_voice"),
])
def test_only_generation_endpoints_apply_headers(model_client, monkeypatch, endpoint, payload, method):
    def service(*args, **kwargs):
        assert factory.get_llm_model() == GPT
        assert bedrock_image._resolved_image_model_id() == ULTRA
        if method == "generate_post":
            return {"prompt": GPT}
        if method == "generate_image":
            return {"imageUrl": "https://test/image.png", "source": "generated"}
        return {"platform": "facebook", "caption": GPT}

    monkeypatch.setattr(social_router.SocialMediaService, method, service)
    response = model_client.post(f"{ROOT}/{endpoint}", json=payload, headers={"X-AI-Text-Model": GPT, "X-AI-Image-Model": ULTRA})
    assert response.status_code == 200, response.text
    assert factory.get_llm_model() == "configured-default"


def test_endpoint_context_cleans_up_after_service_failure(model_client, monkeypatch):
    def service(*args, **kwargs):
        if factory.get_llm_model() == GPT:
            raise HTTPException(status_code=502, detail="Mock generation failed")
        return {"prompt": factory.get_llm_model()}

    monkeypatch.setattr(social_router.SocialMediaService, "generate_post", service)
    payload = {"topic": "test topic", "platforms": ["facebook"]}
    assert model_client.post(f"{ROOT}/generate", json=payload, headers={"X-AI-Text-Model": GPT}).status_code == 502
    response = model_client.post(f"{ROOT}/generate", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["prompt"] == "configured-default"


def test_concurrent_requests_have_isolated_models(model_client, monkeypatch):
    barrier = Barrier(2, timeout=10)

    def service(*args, **kwargs):
        text = factory.get_llm_model()
        image = bedrock_image._resolved_image_model_id()
        barrier.wait()
        assert factory.get_llm_model() == text
        assert bedrock_image._resolved_image_model_id() == image
        return {"prompt": f"{text}|{image}"}

    monkeypatch.setattr(social_router.SocialMediaService, "generate_post", service)

    def request(text, image):
        response = model_client.post(f"{ROOT}/generate", json={"topic": "test topic", "platforms": ["facebook"]}, headers={"X-AI-Text-Model": text, "X-AI-Image-Model": image})
        assert response.status_code == 200, response.text
        return response.json()["prompt"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(request, GPT, SD)
        second = pool.submit(request, GPT_SMALL, ULTRA)
        assert first.result() == f"{GPT}|{SD}"
        assert second.result() == f"{GPT_SMALL}|{ULTRA}"
    assert settings.bedrock_model_id == "configured-default"
    assert settings.bedrock_image_model_id == SD


def test_invalid_header_does_not_call_service(model_client, monkeypatch):
    service = Mock()
    monkeypatch.setattr(social_router.SocialMediaService, "generate_post", service)
    response = model_client.post(f"{ROOT}/generate", json={"topic": "test topic", "platforms": ["facebook"]}, headers={"X-AI-Text-Model": "gpt-5"})
    assert response.status_code == 422
    service.assert_not_called()


def test_unrelated_endpoint_ignores_headers(model_client, monkeypatch):
    def get_voice(*args, **kwargs):
        assert factory.get_llm_model() == "configured-default"
        return {"workspaceId": "workspace"}

    monkeypatch.setattr(social_router.SocialMediaService, "get_brand_voice", get_voice)
    response = model_client.get(f"{ROOT}/brand-voice", headers={"X-AI-Text-Model": "arbitrary"})
    assert response.status_code == 200, response.text


def test_catalog_contract_and_headers_do_not_change_defaults(model_client):
    response = model_client.get(f"{ROOT}/ai-models", headers={"X-AI-Text-Model": GPT})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["defaultTextModelId"] == "configured-default"
    for model in body["textModels"] + body["imageModels"]:
        assert {"id", "label", "provider", "region", "availability", "invokeAccessVerified"} <= model.keys()


def test_catalog_requires_workspace_auth(client):
    prefix = settings.api_v1_prefix
    assert client.get(f"{prefix}{ROOT}/ai-models").status_code in (401, 403)
    registered = client.post(f"{prefix}/auth/register", json={"email": "models@example.com", "password": "s3cret-pass"}).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    workspace = registered["workspace"]["id"]
    assert client.get(f"{prefix}/workspaces/{workspace}/social/ai-models", headers=headers).status_code == 200
    assert client.get(f"{prefix}/workspaces/another/social/ai-models", headers=headers).status_code in (403, 404)