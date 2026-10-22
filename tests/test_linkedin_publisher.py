from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from app.social.publishers.linkedin import LinkedInPublisher, _author_urn


TOKEN = "test-token-not-a-real-secret"
IMAGE_URL = "https://storage.example/image.png?signature=test-signed-url"
UPLOAD_URL = "https://api.linkedin.com/mediaUpload/test?signature=test-upload-url"
ASSET = "urn:li:digitalmediaAsset:test-image"
UGC_URL = "https://api.linkedin.com/v2/ugcPosts"
REGISTER_URL = "https://api.linkedin.com/v2/assets?action=registerUpload"


@pytest.fixture
def image_bytes():
    def make(fmt="PNG"):
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(buffer, format=fmt)
        return buffer.getvalue()

    return make


@pytest.fixture
def mock_http(monkeypatch):
    original_client = httpx.Client

    def install(replies):
        requests = []
        pending = iter(replies)

        def handle(request):
            requests.append(request)
            reply = next(pending)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def client(**kwargs):
            assert kwargs["timeout"] == 60.0
            return original_client(**kwargs, transport=httpx.MockTransport(handle))

        monkeypatch.setattr("app.social.publishers.linkedin.httpx.Client", client)
        return requests

    return install


def registration(**changes):
    value = {
        "asset": ASSET,
        "uploadMechanism": {
            "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                "uploadUrl": UPLOAD_URL,
                "headers": {"X-Upload-Test": "required"},
            }
        },
    }
    value.update(changes)
    return httpx.Response(200, json={"value": value})


def publish(**changes):
    kwargs = {
        "platform_account_id": "person:member",
        "access_token": TOKEN,
        "caption": "A launch",
        "hashtags": ["news", "#team"],
        "image_url": IMAGE_URL,
    }
    kwargs.update(changes)
    return LinkedInPublisher().publish(**kwargs)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("member", "urn:li:person:member"),
        ("person:member", "urn:li:person:member"),
        ("urn:li:person:member", "urn:li:person:member"),
        ("workspace:123", "urn:li:organization:123"),
        ("urn:li:workspace:123", "urn:li:organization:123"),
        ("organization:123", "urn:li:organization:123"),
        ("urn:li:organization:123", "urn:li:organization:123"),
    ],
)
def test_author_contracts(stored, expected):
    assert _author_urn(stored) == expected


@pytest.mark.parametrize("account", ["person:member", "workspace:123", "organization:123"])
@pytest.mark.parametrize(("fmt", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_image_publish(mock_http, image_bytes, account, fmt, mime):
    content = image_bytes(fmt)
    requests = mock_http([
        httpx.Response(200, headers={"Content-Type": f"{mime}; charset=binary"}, content=content),
        registration(),
        httpx.Response(201),
        httpx.Response(201, headers={"x-restli-id": "urn:li:ugcPost:published"}),
    ])
    result = publish(platform_account_id=account, first_comment="Ignored")
    assert result.success
    assert result.platform_post_id == "urn:li:ugcPost:published"
    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", IMAGE_URL), ("POST", REGISTER_URL), ("PUT", UPLOAD_URL), ("POST", UGC_URL)
    ]
    assert "authorization" not in requests[0].headers
    registered = json.loads(requests[1].content)["registerUploadRequest"]
    assert registered == {
        "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
        "owner": _author_urn(account),
        "serviceRelationships": [{"relationshipType": "OWNER", "identifier": "urn:li:userGeneratedContent"}],
    }
    assert requests[2].content == content
    assert requests[2].headers["content-type"] == mime
    assert requests[2].headers["X-Upload-Test"] == "required"
    for request in requests[1:]:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(requests[3].content)
    assert body["author"] == registered["owner"]
    assert body["lifecycleState"] == "PUBLISHED"
    assert body["visibility"] == {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
    assert body["specificContent"]["com.linkedin.ugc.ShareContent"] == {
        "shareCommentary": {"text": "A launch\n\n#news #team"},
        "shareMediaCategory": "IMAGE",
        "media": [{"status": "READY", "media": ASSET}],
    }


@pytest.mark.parametrize("response", [
    httpx.Response(201, json={"id": "published"}),
    httpx.Response(201, headers={"x-restli-id": "published"}),
])
def test_text_only(mock_http, response):
    requests = mock_http([response])
    result = publish(image_url=None, caption="x" * 3100, hashtags=[], first_comment="Ignored")
    assert result.success and result.platform_post_id == "published"
    assert len(requests) == 1
    assert str(requests[0].url) == UGC_URL
    assert json.loads(requests[0].content)["specificContent"]["com.linkedin.ugc.ShareContent"] == {
        "shareCommentary": {"text": "x" * 3000}, "shareMediaCategory": "NONE"
    }


def test_empty_caption_no_requests(mock_http):
    requests = mock_http([])
    result = publish(caption="", hashtags=[])
    assert not result.success and result.error_code == "INVALID_CONTENT"
    assert not requests


@pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "file:///image.png", "ftp://storage.example/image.png", "not a URL"])
def test_invalid_image_url_no_requests(mock_http, url):
    requests = mock_http([])
    result = publish(image_url=url)
    assert not result.success and result.error_code == "INVALID_IMAGE"
    assert not result.retryable
    assert not requests


@pytest.mark.parametrize("case", ["mime", "missing_mime", "empty", "corrupt", "mismatch", "length", "bad_length", "stream_limit"])
def test_invalid_media_never_posts(mock_http, image_bytes, case):
    content = image_bytes()
    headers = {"content-type": "image/png"}
    if case == "mime":
        headers["content-type"] = "image/webp"
    elif case == "missing_mime":
        headers = {}
    elif case == "empty":
        content = b""
    elif case == "corrupt":
        content = b"not an image"
    elif case == "mismatch":
        content = image_bytes("JPEG")
    elif case == "length":
        headers["content-length"] = str(10 * 1024 * 1024 + 1)
    elif case == "bad_length":
        headers["content-length"] = "invalid"
    elif case == "stream_limit":
        content = b"x" * (10 * 1024 * 1024 + 1)
    response = httpx.Response(200, headers=headers, content=content)
    if case == "stream_limit":
        del response.headers["content-length"]
    requests = mock_http([response])
    result = publish()
    assert not result.success and result.error_code == "INVALID_IMAGE"
    assert not result.retryable
    assert len(requests) == 1


@pytest.mark.parametrize("stage", ["download", "register", "upload", "post"])
@pytest.mark.parametrize(("status", "code", "retryable"), [
    (302, "API_ERROR", False),
    (400, "API_ERROR", False),
    (401, "TOKEN_EXPIRED", False),
    (403, "PERMISSION_DENIED", False),
    (429, "RATE_LIMITED", True),
    (503, "API_ERROR", True),
])
def test_http_errors_fail_closed(mock_http, image_bytes, caplog, stage, status, code, retryable):
    stages = ["download", "register", "upload", "post"]
    index = stages.index(stage)
    replies = [
        httpx.Response(200, headers={"content-type": "image/png"}, content=image_bytes()),
        registration(),
        httpx.Response(201),
    ][:index]
    replies.append(httpx.Response(status, text=f"{TOKEN} {IMAGE_URL} {UPLOAD_URL}"))
    requests = mock_http(replies)
    result = publish()
    assert not result.success and result.error_code == code
    assert result.retryable == retryable
    assert len(requests) == index + 1
    if stage != "post":
        assert all(str(r.url) != UGC_URL for r in requests)
    publisher_logs = "\n".join(
        record.getMessage() for record in caplog.records
        if record.name == "app.social.publishers.linkedin"
    )
    assert TOKEN not in caplog.text
    for secret in (TOKEN, IMAGE_URL, UPLOAD_URL):
        assert secret not in (result.error_message or "") + publisher_logs


@pytest.mark.parametrize("index", range(4))
def test_transport_errors_no_secret_leak(mock_http, image_bytes, caplog, index):
    replies = [
        httpx.Response(200, headers={"content-type": "image/png"}, content=image_bytes()),
        registration(),
        httpx.Response(201),
    ][:index]
    replies.append(httpx.ReadTimeout(f"{TOKEN} {IMAGE_URL} {UPLOAD_URL}"))
    requests = mock_http(replies)
    result = publish()
    assert not result.success and result.error_code == "API_ERROR" and result.retryable
    assert len(requests) == index + 1
    publisher_logs = "\n".join(
        record.getMessage() for record in caplog.records
        if record.name == "app.social.publishers.linkedin"
    )
    assert TOKEN not in caplog.text
    for secret in (TOKEN, IMAGE_URL, UPLOAD_URL):
        assert secret not in (result.error_message or "") + publisher_logs


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not JSON"),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"value": None}),
    registration(asset=""),
    registration(asset=123),
    registration(uploadMechanism={}),
    registration(uploadMechanism={"com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {"uploadUrl": "file:///upload"}}),
    registration(uploadMechanism={"com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {"uploadUrl": None}}),
])
def test_invalid_registration_no_upload_or_post(mock_http, image_bytes, response):
    requests = mock_http([
        httpx.Response(200, headers={"content-type": "image/png"}, content=image_bytes()),
        response,
    ])
    result = publish()
    assert not result.success and result.error_code == "API_ERROR"
    assert not result.retryable
    assert len(requests) == 2


def test_storage_redirect_download(mock_http, image_bytes):
    requests = mock_http([
        httpx.Response(302, headers={"location": "https://cdn.example/image.png"}),
        httpx.Response(200, headers={"content-type": "image/png"}, content=image_bytes()),
        registration(), httpx.Response(201), httpx.Response(201, json={"id": "published"}),
    ])
    assert publish().success
    assert str(requests[1].url) == "https://cdn.example/image.png"
    assert "authorization" not in requests[1].headers


@pytest.mark.parametrize("post_id", ["123", "ugcPost:123", "urn:li:ugcPost:123"])
def test_delete_preserved(mock_http, post_id):
    requests = mock_http([httpx.Response(204)])
    result = LinkedInPublisher().delete_remote(
        platform_post_id=post_id, platform_account_id="workspace:123", access_token=TOKEN
    )
    assert result.success and result.platform_post_id == "urn:li:ugcPost:123"
    assert len(requests) == 1 and requests[0].method == "DELETE"
    assert str(requests[0].url) == "https://api.linkedin.com/rest/posts/urn%3Ali%3AugcPost%3A123"


def test_delete_missing_id(mock_http):
    requests = mock_http([])
    result = LinkedInPublisher().delete_remote(
        platform_post_id="", platform_account_id="member", access_token=TOKEN
    )
    assert not result.success and result.error_code == "INVALID_CONTENT"
    assert not requests