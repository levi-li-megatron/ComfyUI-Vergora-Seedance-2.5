# -*- coding: utf-8 -*-
"""Offline tests for the Seedance nodes. No network, API key, or billing."""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import nodes  # noqa: E402


STATE = {
    "posts": [],
    "gets": 0,
    "downloads": [],
    "short_downloads": 0,
    "poll_errors": 0,
}
OUT_DIR = tempfile.mkdtemp(prefix="vergora_seedance_test_")


class FakeFolderPaths:
    @staticmethod
    def get_output_directory():
        return OUT_DIR


def fake_post_json(url, headers, payload, timeout=120):
    STATE["posts"].append({"url": url, "headers": headers, "payload": payload})
    return 200, '{"task_info":{"id":"task-1234","status":"pending"}}'


def fake_get_json(url, headers, timeout=60):
    STATE["gets"] += 1
    assert headers["Authorization"] == "Bearer sk-test", headers
    assert "Content-Type" not in headers, headers
    if STATE["poll_errors"]:
        STATE["poll_errors"] -= 1
        return 503, '{"error":{"message":"temporary gateway error"}}'
    if STATE["gets"] == 1:
        return 200, '{"task_info":{"status":"processing"}}'
    return 200, (
        '{"task_info":{"status":"completed"},'
        '"videos":["https://cdn.example/result.mp4"],'
        '"last_frame_url":"https://cdn.example/last.png"}'
    )


def fake_native_get_json(url, headers, timeout=60):
    STATE["gets"] += 1
    if STATE["gets"] == 1:
        return 200, '{"id":"cgt-123","status":"running"}'
    return 200, (
        '{"id":"cgt-123","status":"succeeded",'
        '"content":{"video_url":"https://cdn.example/result.mov",'
        '"last_frame_url":"https://cdn.example/last.jpg"}}'
    )


def fake_draft_dropped_get_json(url, headers, timeout=60):
    STATE["gets"] += 1
    return 200, (
        '{"id":"task-draft","status":"processing","draft":false,'
        '"resolution":"480p"}'
    )


def fake_download(url, dest_path, timeout=600):
    STATE["downloads"].append(url)
    if STATE["short_downloads"]:
        STATE["short_downloads"] -= 1
        with open(dest_path, "wb") as handle:
            handle.write(b"")
        return
    with open(dest_path, "wb") as handle:
        if "last" in url:
            handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        else:
            handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 9000)


def prepare():
    nodes.http_post_json = fake_post_json
    nodes.http_get_json = fake_get_json
    nodes.http_download = fake_download
    nodes.folder_paths = FakeFolderPaths()
    nodes.time.sleep = lambda _seconds: None
    nodes.load_config = lambda: {
        "base_url": "https://api.vergora.ai/v1",
        "api_key": "sk-test",
        "request_format": "flat",
        "poll_interval": 0.001,
        "timeout": 30,
        "output_dir": "",
    }
    return nodes.VergoraSeedance20(), nodes.VergoraSeedance25()


def reset():
    STATE["posts"] = []
    STATE["gets"] = 0
    STATE["downloads"] = []
    STATE["short_downloads"] = 0
    STATE["poll_errors"] = 0


def expect_error(fn, needle):
    try:
        fn()
    except RuntimeError as exc:
        assert needle in str(exc), "expected %r in %r" % (needle, str(exc))
        return
    raise AssertionError("expected RuntimeError containing %r" % needle)


def base20(**overrides):
    values = {
        "prompt": "a paper boat on a lake",
        "model": "seedance-2.0",
        "resolution": "720p",
        "ratio": "adaptive",
        "duration": 5,
        "generate_audio": True,
        "watermark": False,
        "return_last_frame": False,
    }
    values.update(overrides)
    return values


def base25(**overrides):
    values = {
        "prompt": "a paper boat on a lake",
        "model": "seedance-2.5",
        "resolution": "720p",
        "ratio": "adaptive",
        "duration": 5,
        "generate_audio": True,
        "watermark": False,
        "return_last_frame": False,
        "output_format": "mp4",
        "omni_reference_task_type": "auto",
        "draft": False,
    }
    values.update(overrides)
    return values


def run_node(node, values, **optional):
    return node.run(**values, **optional)


def main():
    node20, node25 = prepare()
    assert set(nodes.NODE_CLASS_MAPPINGS) == {"VergoraSeedance25"}, nodes.NODE_CLASS_MAPPINGS

    # Seedance 2.x does not support the Seedance 1.x seed/camera controls.
    for node_class in (nodes.VergoraSeedance20, nodes.VergoraSeedance25):
        schema = node_class.INPUT_TYPES()
        input_names = set(schema["required"]) | set(schema["optional"])
        assert "seed" not in input_names and "camera_fixed" not in input_names, input_names

    # Model IDs copied from the catalog may carry an invisible zero-width space.
    clean, _spec = nodes.model_spec("seedance-2.0-fast\u200b")
    assert clean == "seedance-2.0-fast", repr(clean)

    # Seedance 2.0: flat gateway body, polling, video preview and last frame.
    reset()
    out = run_node(
        node20,
        base20(
            model="seedance-2.0-fast\u200b",
            resolution="720p",
            return_last_frame=True,
        ),
    )
    assert len(STATE["posts"]) == 1, STATE["posts"]
    submitted = STATE["posts"][0]
    assert submitted["url"] == "https://api.vergora.ai/v1/videos", submitted
    assert submitted["headers"]["Content-Type"] == "application/json", submitted
    p = submitted["payload"]
    assert p["model"] == "seedance-2.0-fast", p
    assert p["prompt"] == "a paper boat on a lake", p
    assert p["resolution"] == "720p" and p["ratio"] == "adaptive" and p["duration"] == 5, p
    assert p["generate_audio"] is True and p["watermark"] is False, p
    assert "seed" not in p and "camera_fixed" not in p, p
    assert p["return_last_frame"] is True, p
    assert "output_format" not in p and "omni_reference_task_type" not in p, p
    assert STATE["gets"] == 2, STATE["gets"]
    result = out["result"]
    assert len(result) == 4 and os.path.exists(result[0]) and os.path.exists(result[3]), result
    assert result[1] == "task-1234" and result[2].endswith("result.mp4"), result
    preview = out["ui"]["images"][0]
    assert preview["format"] == "video/mp4", preview

    # Native response shapes are accepted too.
    reset()
    nodes.http_get_json = fake_native_get_json
    nodes.load_config = lambda: {
        "base_url": "https://api.vergora.ai/v1",
        "api_key": "sk-test",
        "request_format": "content",
        "poll_interval": 0.001,
        "timeout": 30,
        "output_dir": "",
    }
    out25 = run_node(
        node25,
        base25(resolution="1080p", duration=30, output_format="mov", return_last_frame=True),
    )
    p = STATE["posts"][0]["payload"]
    assert p["model"] == "seedance-2.5" and p["duration"] == 30, p
    assert p["output_format"] == "mov" and p["omni_reference_task_type"] == "auto", p
    assert "seed" not in p and "camera_fixed" not in p, p
    assert p["content"] == [{"type": "text", "text": "a paper boat on a lake"}], p
    native_result = out25["result"]
    assert native_result[0].endswith(".mov"), native_result
    assert os.path.exists(native_result[3]), native_result

    # Native content[] roles, including all three media types.
    nodes.load_config = lambda: {
        "base_url": "https://api.vergora.ai/v1",
        "api_key": "sk-test",
        "request_format": "content",
        "poll_interval": 0.001,
        "timeout": 30,
        "output_dir": "",
    }
    p = nodes.build_payload(
        "Use Image 1, Video 1 and Audio 1",
        "seedance-2.5",
        "720p",
        "adaptive",
        8,
        True,
        False,
        False,
        first_frame=None,
        last_frame=None,
        reference_images=["https://example.com/a.png"],
        reference_videos=["https://example.com/b.mp4"],
        reference_audios=["https://example.com/c.mp3"],
        omni_reference_task_type="reference",
        request_format="content",
    )
    roles = [item.get("role") for item in p["content"] if item["type"] != "text"]
    assert roles == ["reference_image", "reference_video", "reference_audio"], p["content"]

    # First and last frame roles.
    p = nodes.build_payload(
        "morph",
        "seedance-2.0",
        "480p",
        "16:9",
        4,
        True,
        False,
        False,
        first_frame="https://example.com/first.png",
        last_frame="https://example.com/last.png",
        request_format="content",
    )
    assert p["content"][1]["role"] == "first_frame", p
    assert p["content"][2]["role"] == "last_frame", p

    # Model-specific resolution and duration limits.
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0-fast", "1080p", "adaptive", 5, True, False,
            False, request_format="flat"
        ),
        "不支持",
    )
    nodes.build_payload(
        "x", "seedance-2.0", "4k", "adaptive", 15, True, False,
        False, request_format="flat"
    )
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0", "720p", "adaptive", 16, True, False,
            False, request_format="flat"
        ),
        "duration",
    )

    # Seedance 2.0 audio-only and text+audio are not supported.
    expect_error(
        lambda: nodes.build_payload(
            "music", "seedance-2.0", "720p", "adaptive", 5, True, False,
            False, reference_audios=["https://example.com/a.mp3"],
            request_format="flat"
        ),
        "纯音频",
    )
    nodes.build_payload(
        "music", "seedance-2.5", "720p", "adaptive", 5, True, False,
        False, reference_audios=["https://example.com/a.mp3"],
        request_format="flat"
    )

    # Media limits are model-specific.
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0", "720p", "adaptive", 5, True, False,
            False,
            reference_images=["https://example.com/%d.png" % i for i in range(10)],
            request_format="flat"
        ),
        "最多 9",
    )
    nodes.build_payload(
        "x", "seedance-2.5", "720p", "adaptive", 5, True, False,
        False,
        reference_images=["https://example.com/%d.png" % i for i in range(30)],
        reference_videos=["https://example.com/%d.mp4" % i for i in range(10)],
        reference_audios=["https://example.com/%d.mp3" % i for i in range(10)],
        request_format="flat"
    )

    # Task constraints on Seedance 2.5.
    expect_error(
        lambda: nodes.build_payload(
            "edit it", "seedance-2.5", "720p", "adaptive", 5, True, False,
            False, reference_videos=["https://example.com/a.mp4"],
            omni_reference_task_type="edit", request_format="flat"
        ),
        "duration=-1",
    )
    nodes.build_payload(
        "edit it", "seedance-2.5", "720p", "adaptive", -1, True, False,
        False, reference_videos=["https://example.com/a.mp4"],
        omni_reference_task_type="edit", request_format="flat"
    )
    expect_error(
        lambda: nodes.build_payload(
            "extend it", "seedance-2.5", "720p", "16:9", 5, True, False,
            False, reference_videos=["https://example.com/a.mp4"],
            omni_reference_task_type="extend", request_format="flat"
        ),
        "ratio=adaptive",
    )
    expect_error(
        lambda: nodes.build_payload(
            "animate", "seedance-2.5", "720p", "16:9", 5, True, False,
            False, first_frame="https://example.com/a.png",
            request_format="flat"
        ),
        "ratio=adaptive",
    )
    expect_error(
        lambda: nodes.build_payload(
            "draft", "seedance-2.5", "720p", "adaptive", 5, True, False,
            False, draft=True, request_format="flat"
        ),
        "480p",
    )
    draft = nodes.build_payload(
        "draft", "seedance-2.5", "480p", "adaptive", 5, True, False,
        False, draft=True, request_format="flat"
    )
    assert draft["draft"] is True and draft["resolution"] == "480p", draft

    final = nodes.build_payload(
        "", "seedance-2.5", "1080p", "adaptive", 5, True, False,
        False, draft_task_id="cgt-draft-1", output_format="mov",
        request_format="flat"
    )
    assert final["content"] == [
        {"type": "draft_task", "draft_task": {"id": "cgt-draft-1"}}
    ], final
    assert "duration" not in final and final["resolution"] == "1080p", final
    assert "generate_audio" not in final and "omni_reference_task_type" not in final, final
    expect_error(
        lambda: nodes.build_payload(
            "", "seedance-2.5", "720p", "adaptive", 5, True, False,
            False, draft_task_id="cgt-draft-1", request_format="flat"
        ),
        "1080p",
    )

    # Keyframe and reference modes cannot be mixed. Last frame needs a first frame.
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0", "720p", "16:9", 5, True, False,
            False, first_frame="https://example.com/f.png",
            reference_images=["https://example.com/r.png"], request_format="flat"
        ),
        "不能混用",
    )
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0", "720p", "adaptive", 5, True, False,
            False, first_frame="https://example.com/f.png", request_format="flat"
        ),
        "网关不接受图生视频 ratio=adaptive",
    )
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.0", "720p", "adaptive", 5, True, False,
            False, last_frame="https://example.com/l.png", request_format="flat"
        ),
        "requires first_frame",
    )

    # extra_json is merged last and must be a JSON object.
    p = nodes.build_payload(
        "x", "seedance-2.5", "720p", "adaptive", 5, True, False,
        False, extra_json='{"service_tier":"default","priority":3}',
        request_format="flat"
    )
    assert p["service_tier"] == "default" and p["priority"] == 3, p
    expect_error(
        lambda: nodes.build_payload(
            "x", "seedance-2.5", "720p", "adaptive", 5, True, False,
            False, extra_json="[]", request_format="flat"
        ),
        "JSON 对象",
    )

    # Polling heals transient errors.
    reset()
    STATE["poll_errors"] = 2
    nodes.http_get_json = fake_get_json
    nodes.load_config = lambda: {
        "base_url": "https://api.vergora.ai/v1",
        "api_key": "sk-test",
        "request_format": "flat",
        "poll_interval": 0.001,
        "timeout": 30,
        "output_dir": "",
    }
    url, last = nodes.wait_for_video(nodes.load_config(), "task-1234")
    assert url.endswith(".mp4") and last.endswith(".png"), (url, last)

    # A stalled transfer is retried, not accepted as a zero-byte file.
    path = os.path.join(OUT_DIR, "retry.mp4")
    STATE["short_downloads"] = 1
    nodes.download_asset("https://cdn.example/retry.mp4", path, nodes.MIN_VIDEO_BYTES)
    assert os.path.getsize(path) > nodes.MIN_VIDEO_BYTES, path
    assert not os.path.exists(path + ".part"), path

    # Actionable HTTP errors.
    hint = nodes.explain_http_error(
        401, '{"error":{"message":"Invalid api key"}}'
    )
    assert "API key" in hint and "鉴权" in hint, hint
    hint = nodes.explain_http_error(
        400, '{"error":{"message":"content is required"}}'
    )
    assert "REQUEST_FORMAT = content" in hint, hint
    hint = nodes.explain_http_error(
        402,
        '{"detail":"Insufficient balance. Required: 1000000 amount (100.0 credits), '
        'Available: 750000 amount (75.0 credits)"}',
    )
    assert "100.0" in hint and "75.0" in hint, hint
    hint = nodes.explain_http_error(
        403,
        '{"error":{"message":"user quota is not enough","code":"insufficient_quota"}}',
    )
    assert "配额不足" in hint and "Key 本身可能有效" in hint, hint
    hint = nodes.explain_http_error(
        400,
        '{"error":{"message":"invalid video ratio","code":"estimate_video_tokens_failed"}}',
    )
    assert "REQUEST_FORMAT = content" in hint and "任务尚未创建" in hint, hint
    hint = nodes.explain_http_error(
        400,
        '{"error":{"message":"invalid video ratio","code":"estimate_video_tokens_failed"}}',
        request_format="content",
        draft=True,
    )
    assert "已经是 content[]" in hint and "Draft" in hint, hint
    assert "设置 REQUEST_FORMAT = content" not in hint, hint
    hint = nodes.explain_http_error(
        400,
        '{"error":{"message":"invalid video ratio","code":"estimate_video_tokens_failed"}}',
        request_format="flat",
        task_type="edit",
        has_reference_video=True,
    )
    assert "视频编辑/扩展" in hint and "固定画幅" in hint, hint
    assert "不是素材 URL" in hint, hint
    hint = nodes.explain_http_error(
        400,
        '{"error":{"message":"invalid video ratio","code":"estimate_video_tokens_failed"}}',
        request_format="content",
        has_first_frame=True,
    )
    assert "首帧/首尾帧" in hint and "不是首帧图片不可访问" in hint, hint
    hint = nodes.explain_http_error(
        503,
        '{"error":{"message":"No available channel for model seedance-2.0-fast"}}',
    )
    assert "没有该模型的可用通道" in hint, hint

    failed = {
        "id": "cgt-x",
        "status": "failed",
        "error": {
            "code": "InvalidParameter.TaskTypeConstraint",
            "message": "video editing requires duration -1",
        },
    }
    message = nodes.explain_task_failure(failed, json.dumps(failed))
    assert "omni_reference_task_type=edit" in message, message

    not_draft = {
        "id": "cgt-x",
        "status": "failed",
        "error": {"message": "draft_task is not a draft"},
    }
    message = nodes.explain_task_failure(not_draft, json.dumps(not_draft))
    assert "draft=false" in message and "不要重复提交" in message, message

    # The current gateway echoes draft=false on a task submitted with draft=true.
    reset()
    nodes.http_get_json = fake_draft_dropped_get_json
    expect_error(
        lambda: nodes.wait_for_video(
            {
                "base_url": "https://api.vergora.ai/v1",
                "api_key": "sk-test",
                "poll_interval": 0.001,
                "timeout": 30,
            },
            "task-draft",
            expect_draft=True,
        ),
        "draft=false",
    )
    assert STATE["gets"] == 1, STATE["gets"]

    # Submits must not be retried because a duplicate could be billed twice.
    calls = {"n": 0}

    def broken_post(url, headers, payload, timeout=120):
        calls["n"] += 1
        raise RuntimeError("SSLError(UNEXPECTED_EOF_WHILE_READING)")

    nodes.http_post_json = broken_post
    expect_error(
        lambda: nodes.submit_task(nodes.load_config(), {"model": "seedance-2.0", "prompt": "x"}),
        "没有自动重试",
    )
    assert calls["n"] == 1, calls

    # auto mode retries a submit once, but only for an explicit body-shape error.
    reset()
    schema_calls = []

    def schema_then_success(url, headers, payload, timeout=120):
        schema_calls.append(payload)
        if len(schema_calls) == 1:
            return 400, '{"error":{"message":"unknown field: prompt"}}'
        return 200, '{"task_info":{"id":"task-auto","status":"pending"}}'

    nodes.http_post_json = schema_then_success
    nodes.http_get_json = fake_get_json
    nodes.load_config = lambda: {
        "base_url": "https://api.vergora.ai/v1",
        "api_key": "sk-test",
        "request_format": "auto",
        "poll_interval": 0.001,
        "timeout": 30,
        "output_dir": "",
    }
    run_node(node20, base20())
    assert len(schema_calls) == 2, schema_calls
    assert "prompt" in schema_calls[0] and "content" not in schema_calls[0], schema_calls
    assert "content" in schema_calls[1] and "prompt" not in schema_calls[1], schema_calls

    ambiguous_calls = []

    def ambiguous_bad_request(url, headers, payload, timeout=120):
        ambiguous_calls.append(payload)
        return 400, '{"error":{"message":"ratio is not supported"}}'

    nodes.http_post_json = ambiguous_bad_request
    expect_error(lambda: run_node(node20, base20()), "ratio")
    assert len(ambiguous_calls) == 1, ambiguous_calls

    # Image conversion is exercised when Pillow is available in this interpreter.
    if nodes.Image is not None:
        import numpy as np

        noisy = np.random.RandomState(0).rand(1024, 1024, 3).astype(np.float32)
        uri = nodes.tensor_to_data_uri(noisy, max_bytes=200 * 1024)
        raw = nodes.base64.b64decode(uri.split(",", 1)[1])
        assert len(raw) <= 200 * 1024, len(raw)
        assert uri.startswith("data:image/"), uri[:30]

    print("OK 全部通过 / all checks passed")
    print("  nodes:", ", ".join(nodes.NODE_CLASS_MAPPINGS))
    print("  output:", OUT_DIR)


if __name__ == "__main__":
    main()
