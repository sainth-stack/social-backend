from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.core.mongo_utils import utcnow
from app.social.ai.prompts import PLATFORM_LIMITS
from app.social.models import SocialPlatform
from app.social.publishers.base import format_caption
from app.social.schemas import SchedulePostRequest, UpdateSocialPostRequest
from app.social.service import PUBLISHABLE_PLATFORMS, SocialMediaService


def _post(platform="facebook"):
    now = utcnow()
    return {
        "id": "post",
        "workspace_id": "workspace",
        "created_by": "user",
        "title": "Launch our new product",
        "status": "draft",
        "image_url": "https://cdn.test/image.jpg" if platform == "instagram" else None,
        "image_source": "none",
        "created_at": now,
        "updated_at": now,
    }


def _platform(platform="facebook", **overrides):
    return {
        "id": platform,
        "post_id": "post",
        "platform": platform,
        "social_account_id": f"{platform}-account",
        "caption": "Launch our new product",
        "hashtags": [],
        "status": "pending",
        **overrides,
    }


def test_publishable_platforms_have_canonical_limits():
    assert {platform.value for platform in PUBLISHABLE_PLATFORMS} == set(PLATFORM_LIMITS)
    assert PUBLISHABLE_PLATFORMS == set(SocialPlatform)


@pytest.mark.parametrize("platform", sorted(PLATFORM_LIMITS))
@pytest.mark.parametrize(
    ("hashtags", "suffix"),
    [([], ""), (["launch", "#AI", "", "##SaaS"], "\n\n#launch #AI #SaaS")],
)
@pytest.mark.parametrize("overflow", [0, 1])
def test_final_caption_boundary_uses_publisher_format(db, platform, hashtags, suffix, overflow):
    service = SocialMediaService(db)
    limit = PLATFORM_LIMITS[platform]
    content = "a" * (limit - len(suffix) + overflow)
    caption = " \n" + content + "\n "
    row = _platform(platform, caption=caption, hashtags=hashtags, character_count=0)
    before = deepcopy(row)
    assert format_caption(caption, hashtags) == content + suffix
    assert len(format_caption(caption, hashtags)) == limit + overflow

    if overflow:
        with pytest.raises(HTTPException) as exc:
            service._validate_ready_to_publish_dict(_post(platform), [row])
        assert exc.value.status_code == 400
        assert platform in exc.value.detail
        assert str(limit + overflow) in exc.value.detail
        assert str(limit) in exc.value.detail
        assert "including hashtags" in exc.value.detail
    else:
        service._validate_ready_to_publish_dict(_post(platform), [row])

    assert row == before


@pytest.mark.parametrize("operation", ["publish", "schedule", "update"])
@pytest.mark.parametrize("platform", sorted(PLATFORM_LIMITS))
def test_overflow_has_no_account_resolution_db_mutation_or_queue(db, monkeypatch, operation, platform):
    service = SocialMediaService(db)
    post = _post(platform)
    original_post = deepcopy(post)
    first_platform = "linkedin" if platform == "facebook" else "facebook"
    limit = PLATFORM_LIMITS[platform]
    rows = [
        _platform(first_platform, social_account_id=None),
        _platform(platform, social_account_id=None, caption="a" * (limit - 4), hashtags=["go"]),
    ]
    db["social_posts"].insert_one(deepcopy(post))
    db["social_post_platforms"].insert_many(deepcopy(rows))
    before = {
        name: list(db[name].find()) for name in db.list_collection_names()
    }
    resolve = Mock(side_effect=AssertionError("Account resolution must not run"))
    enqueue = Mock(side_effect=AssertionError("Publish queue must not run"))
    monkeypatch.setattr(service, "_default_account_for_platform", resolve)
    monkeypatch.setattr(service, "_enqueue_publish", enqueue)

    with pytest.raises(HTTPException) as exc:
        if operation == "publish":
            service.publish_now(post)
        elif operation == "schedule":
            service.schedule_post(
                post,
                SchedulePostRequest(scheduledAt=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
            )
        else:
            service.update_post(post, {"id": "workspace"}, UpdateSocialPostRequest(status="scheduled"))

    assert exc.value.status_code == 400
    assert platform in exc.value.detail
    assert str(limit + 1) in exc.value.detail
    resolve.assert_not_called()
    enqueue.assert_not_called()
    assert {
        name: list(db[name].find()) for name in db.list_collection_names()
    } == before
    assert post == original_post


@pytest.mark.parametrize("operation", ["publish", "schedule"])
@pytest.mark.parametrize("platform", sorted(PLATFORM_LIMITS))
def test_at_limit_can_resolve_account_and_enqueue(db, monkeypatch, operation, platform):
    service = SocialMediaService(db)
    post = _post(platform)
    row = _platform(platform, caption="a" * (PLATFORM_LIMITS[platform] - 5), hashtags=["go"], social_account_id=None)
    db["social_posts"].insert_one(post)
    db["social_post_platforms"].insert_one(row)
    db["social_accounts"].insert_one({
        "id": f"{platform}-account", "workspace_id": "workspace", "platform": platform, "is_active": True,
    })
    enqueue = Mock()
    monkeypatch.setattr(service, "_enqueue_publish", enqueue)

    if operation == "publish":
        result = service.publish_now(post)
    else:
        result = service.schedule_post(
            post,
            SchedulePostRequest(scheduledAt=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )

    assert result.platforms[0].socialAccountId == f"{platform}-account"
    assert db["social_post_platforms"].find_one({"id": platform})["social_account_id"] == f"{platform}-account"
    assert result.platforms[0].caption == row["caption"]
    assert result.platforms[0].hashtags == ["go"]
    assert len(format_caption(result.platforms[0].caption, result.platforms[0].hashtags)) == PLATFORM_LIMITS[platform]
    enqueue.assert_called_once()


@pytest.mark.parametrize(
    ("platform", "caption", "image_url", "error"),
    [
        ("instagram", "Caption", None, "Instagram posts require an image URL"),
        ("x", "Caption", "https://cdn.test/image.jpg", "X free tier is text-only"),
        ("facebook", "Caption", "https://cdn.test/video.mp4", "Video publishing is not supported yet"),
        ("linkedin", "Caption", "https://cdn.test/video.mp4", "Video publishing is not supported yet"),
        ("facebook", " \n ", None, "facebook content is empty"),
    ],
)
def test_existing_media_and_empty_content_rules(db, platform, caption, image_url, error):
    post = _post(platform) | {"image_url": image_url}
    with pytest.raises(HTTPException) as exc:
        SocialMediaService(db)._validate_ready_to_publish_dict(post, [_platform(platform, caption=caption)])
    assert exc.value.status_code == 400
    assert error in exc.value.detail


@pytest.mark.parametrize("platform", ["facebook", "instagram", "linkedin"])
def test_image_only_content_remains_publishable(db, platform):
    post = _post(platform) | {"image_url": "https://cdn.test/image.jpg"}
    SocialMediaService(db)._validate_ready_to_publish_dict(post, [_platform(platform, caption="")])


@pytest.mark.parametrize("rows", [[], [_platform("unsupported", caption="a" * 10000)]])
def test_no_publishable_content_is_rejected(db, rows):
    with pytest.raises(HTTPException) as exc:
        SocialMediaService(db)._validate_ready_to_publish_dict(_post(), rows)
    assert exc.value.status_code == 400
    assert "no platform content" in exc.value.detail or "publishable account" in exc.value.detail


def test_unsupported_platform_does_not_block_available_platform(db):
    SocialMediaService(db)._validate_ready_to_publish_dict(
        _post(), [_platform("unsupported", caption="a" * 10000), _platform()],
    )


@pytest.mark.parametrize("regenerate_caption", [False, True])
@pytest.mark.parametrize("first_comment", [None, "", "  Keep my first comment #launch  "])
def test_regeneration_preserves_existing_first_comment(db, monkeypatch, regenerate_caption, first_comment):
    import app.plans.service as plans
    import app.social.limits as limits
    import app.social.media as media
    import app.social.service as service_module
    from app.social.polish import SocialPolishService

    service = SocialMediaService(db)
    post = _post("instagram")
    row = _platform("instagram", social_account_id=None, first_comment=first_comment)
    db["social_posts"].insert_one(post)
    db["social_post_platforms"].insert_one(row)
    monkeypatch.setattr(SocialPolishService, "get_settings", lambda *args: {})
    monkeypatch.setattr(limits, "enforce_ai_text_limit", lambda *args: None)
    monkeypatch.setattr(limits, "enforce_ai_image_limit", lambda *args: None)
    monkeypatch.setattr(plans, "record_ai_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "render_brand_creative", lambda *args: SimpleNamespace(image=b"image"))
    monkeypatch.setattr(media, "upload_social_image_bytes", lambda *args, **kwargs: None)
    generate = Mock(return_value={
        "instagram": {"caption": "New caption", "hashtags": ["new"], "firstComment": ""},
    })
    monkeypatch.setattr(service_module, "generate_platform_content", generate)

    result = service.regenerate_post_content(
        post, {"id": "workspace"}, {"id": "user"},
        regenerate_caption=regenerate_caption, regenerate_image=not regenerate_caption,
    )

    assert result.platforms[0].firstComment == first_comment
    assert db["social_post_platforms"].find_one({"post_id": "post"})["first_comment"] == first_comment
    assert result.platforms[0].caption == ("New caption" if regenerate_caption else row["caption"])
    if regenerate_caption:
        generate.assert_called_once()
        assert generate.call_args.kwargs["include_comment"] is False
    else:
        generate.assert_not_called()