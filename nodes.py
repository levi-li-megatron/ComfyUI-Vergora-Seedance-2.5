# -*- coding: utf-8 -*-
"""ComfyUI node for BytePlus Seedance 2.5 video generation through Vergora.

The node implements the public BytePlus Seedance 2.5 contract.
Vergora exposes asynchronous video jobs through `/v1/videos`; depending on the
deployment, that route may accept the gateway's flat field shape or the native
BytePlus `content[]` shape. `REQUEST_FORMAT` selects which body to send.

The API key is read from config.ini or the environment and is never a node
widget, so it cannot be saved into a ComfyUI workflow.
"""

import base64
import configparser
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from io import BytesIO

import numpy as np

try:
    import requests
except ImportError:  # ComfyUI does not always ship requests
    requests = None

try:
    import folder_paths
except Exception:  # pragma: no cover
    folder_paths = None

try:
    from comfy.model_management import throw_exception_if_processing_interrupted
except Exception:  # pragma: no cover
    def throw_exception_if_processing_interrupted():
        return None

try:
    from comfy.utils import ProgressBar
except Exception:  # pragma: no cover
    ProgressBar = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")

DEFAULT_BASE_URL = "https://api.vergora.ai/v1"
DEFAULT_SUBMIT_TIMEOUT = 300.0
CATEGORY = "vergora"

SEEDANCE_20_MODELS = ["seedance-2.0", "seedance-2.0-fast", "seedance-2.0-mini"]
SEEDANCE_25_MODEL = "seedance-2.5"
SEEDANCE_25_MODELS = [SEEDANCE_25_MODEL]
ALL_MODELS = SEEDANCE_20_MODELS + SEEDANCE_25_MODELS

RATIOS = ["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
OUTPUT_FORMATS = ["mp4", "mov"]
OMNI_TASK_TYPES = ["auto", "reference", "edit", "extend"]
REQUEST_FORMATS = ("auto", "flat", "content")

MODEL_SPECS = {
    "seedance-2.0": {
        "family": "2.0",
        "resolutions": ["4k", "1080p", "720p", "480p"],
        "max_duration": 15,
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
    },
    "seedance-2.0-fast": {
        "family": "2.0",
        "resolutions": ["720p", "480p"],
        "max_duration": 15,
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
    },
    "seedance-2.0-mini": {
        "family": "2.0",
        "resolutions": ["720p", "480p"],
        "max_duration": 15,
        "max_images": 9,
        "max_videos": 3,
        "max_audios": 3,
    },
    "seedance-2.5": {
        "family": "2.5",
        "resolutions": ["1080p", "720p", "480p"],
        "max_duration": 30,
        "max_images": 30,
        "max_videos": 10,
        "max_audios": 10,
    },
}

# Leave room for JSON syntax and Base64 expansion under the gateway's 1 MB body limit.
MAX_BODY_IMAGE_BYTES = 650 * 1024
MIN_VIDEO_BYTES = 8192
MIN_IMAGE_BYTES = 256


def log(message):
    """Print without failing on consoles that cannot encode CJK."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


def make_progress(total=100):
    if ProgressBar is None:
        return None
    try:
        return ProgressBar(total)
    except Exception:  # pragma: no cover
        return None


def tick_progress(progress, value, total=100):
    if progress is None:
        return
    try:
        progress.update_absolute(int(value), total)
    except Exception:  # pragma: no cover
        pass


def normalize_model(model):
    """Remove the invisible characters that can arrive from a copied catalog ID."""
    return str(model or "").replace("\u200b", "").replace("\u200c", "").replace(
        "\u200d", ""
    ).replace("\ufeff", "").strip()


def model_spec(model):
    clean = normalize_model(model)
    spec = MODEL_SPECS.get(clean)
    if spec is None:
        raise RuntimeError(
            "不支持的 Seedance 模型 / unsupported Seedance model: %r. "
            "可选 / use one of: %s" % (model, ", ".join(ALL_MODELS))
        )
    return clean, spec


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def http_post_json(url, headers, payload, timeout=120):
    """Return (status_code, response_text). Submits are never retried here."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if requests is not None:
        response = requests.post(url, headers=headers, data=body, timeout=timeout)
        return response.status_code, response.text
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def http_get_json(url, headers, timeout=60):
    """GET JSON without Content-Type (the gateway rejects an empty JSON body)."""
    if requests is not None:
        response = requests.get(url, headers=headers, timeout=timeout)
        return response.status_code, response.text
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def http_download(url, dest_path, timeout=600):
    if requests is not None:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(dest_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        handle.write(chunk)
        return
    request = urllib.request.Request(url, headers={"User-Agent": "vergora-comfyui-seedance"})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(dest_path, "wb") as handle:
        shutil.copyfileobj(response, handle, length=1 << 20)


def looks_like_network_error(exc):
    text = ("%s %s" % (exc.__class__.__name__, exc)).lower()
    hints = (
        "ssl", "eof", "timed out", "timeout", "connection reset", "connection aborted",
        "connection refused", "remote end closed", "handshake", "temporarily unavailable",
        "network is unreachable", "getaddrinfo", "proxy", "unexpected_eof",
    )
    return any(hint in text for hint in hints)


def looks_like_schema_mismatch(text):
    """Only retry a submit when the gateway explicitly rejects the body shape."""
    lower = (text or "").lower()
    markers = (
        "content is required",
        "content[] is required",
        "missing content",
        "missing required parameter: content",
        "required parameter content",
        "unknown field",
        "unrecognized field",
        "additionalproperties",
        "extra fields not permitted",
        "unsupported field",
        "invalid field: prompt",
        "invalid field: first_frame",
        "invalid field: reference_images",
    )
    return any(marker in lower for marker in markers)


class SubmitSchemaError(RuntimeError):
    """A non-billable request-body mismatch suitable for one format retry."""


def network_error_message(exc, url):
    return (
        "网络中断 / network error while contacting %s: %s\n"
        "提交请求没有自动重试，避免重复创建任务和重复计费。确认本地代理或 VPN 正常后可直接再运行一次。"
        " / Submit requests are deliberately not retried because a duplicate could be billed twice."
        % (url, exc)
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    cfg = {
        "base_url": DEFAULT_BASE_URL,
        "api_key": "",
        "request_format": "auto",
        "poll_interval": 10.0,
        "submit_timeout": DEFAULT_SUBMIT_TIMEOUT,
        "timeout": 2400.0,
        "output_dir": "",
    }
    if os.path.exists(CONFIG_PATH):
        parser = configparser.ConfigParser()
        parser.read(CONFIG_PATH, encoding="utf-8")
        if parser.has_section("API"):
            base = parser.get("API", "BASE_URL", fallback="").strip()
            key = parser.get("API", "KEY", fallback="").strip()
            fmt = parser.get("API", "REQUEST_FORMAT", fallback="").strip()
            if base:
                cfg["base_url"] = base
            if key:
                cfg["api_key"] = key
            if fmt:
                cfg["request_format"] = fmt
        if parser.has_section("RUNTIME"):
            cfg["poll_interval"] = parser.getfloat(
                "RUNTIME", "POLL_INTERVAL", fallback=cfg["poll_interval"]
            )
            cfg["submit_timeout"] = parser.getfloat(
                "RUNTIME", "SUBMIT_TIMEOUT", fallback=cfg["submit_timeout"]
            )
            cfg["timeout"] = parser.getfloat("RUNTIME", "TIMEOUT", fallback=cfg["timeout"])
            out_dir = parser.get("RUNTIME", "OUTPUT_DIR", fallback="").strip()
            if out_dir:
                cfg["output_dir"] = out_dir

    env_base = os.environ.get("VERGORA_BASE_URL", "").strip()
    env_key = os.environ.get("VERGORA_API_KEY", "").strip()
    env_format = os.environ.get("VERGORA_REQUEST_FORMAT", "").strip()
    if env_base:
        cfg["base_url"] = env_base
    if env_key:
        cfg["api_key"] = env_key
    if env_format:
        cfg["request_format"] = env_format

    cfg["base_url"] = cfg["base_url"].rstrip("/")
    cfg["request_format"] = str(cfg["request_format"]).strip().lower()
    if cfg["request_format"] not in REQUEST_FORMATS:
        raise RuntimeError(
            "REQUEST_FORMAT 只能是 auto、flat 或 content / "
            "REQUEST_FORMAT must be auto, flat, or content, got %r"
            % cfg["request_format"]
        )
    return cfg


def auth_headers(cfg, with_json=True):
    if not cfg.get("api_key"):
        raise RuntimeError(
            "没有配置 API key / no API key configured: put it in %s under [API] KEY, "
            "or set VERGORA_API_KEY" % CONFIG_PATH
        )
    headers = {"Authorization": "Bearer " + cfg["api_key"]}
    if with_json:
        headers["Content-Type"] = "application/json"
    return headers


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def encode_frame(frame, max_bytes):
    if Image is None:
        raise RuntimeError("需要 Pillow 才能编码 IMAGE / Pillow is required to encode IMAGE tensors")
    arr = frame
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 4:
        arr = arr[0]
    if arr.dtype != np.uint8:
        arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    image = Image.fromarray(arr)
    if image.mode != "RGB":
        image = image.convert("RGB")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    if buffer.tell() <= max_bytes:
        return buffer.getvalue(), "png"

    for quality in (95, 88, 80, 72, 64):
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue(), "jpeg"

    for scale in (0.8, 0.65, 0.5, 0.4, 0.3, 0.22):
        buffer = BytesIO()
        resized = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
        resized.save(buffer, format="JPEG", quality=82, optimize=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue(), "jpeg"

    raise RuntimeError(
        "图片压缩后仍超过请求体预算 / image still exceeds the request budget; "
        "请改用公开图片链接 / use a hosted image URL instead"
    )


def make_data_uri(payload, kind):
    mime = "image/jpeg" if kind == "jpeg" else "image/png"
    return "data:%s;base64,%s" % (mime, base64.b64encode(payload).decode("ascii"))


def array_to_data_uri(frame, max_bytes=None):
    payload, kind = encode_frame(frame, max_bytes or MAX_BODY_IMAGE_BYTES)
    return make_data_uri(payload, kind)


def count_frames(images):
    if images is None:
        return 0
    arr = images
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.asarray(arr)
    return 1 if arr.ndim == 3 else int(arr.shape[0])


def tensor_to_data_uri(image, max_bytes=None):
    if image is None:
        return None
    arr = image
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 3:
        arr = arr[None, ...]
    return array_to_data_uri(arr[0], max_bytes)


def tensor_batch_to_data_uris(images, limit, max_bytes=None):
    if images is None:
        return []
    arr = images
    if hasattr(arr, "detach"):
        arr = arr.detach().cpu().numpy()
    arr = np.asarray(arr)
    if arr.ndim == 3:
        arr = arr[None, ...]
    return [array_to_data_uri(frame, max_bytes) for frame in arr[:limit]]


def split_uri_lines(text):
    if not text:
        return []
    return [
        line.strip()
        for line in str(text).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# ---------------------------------------------------------------------------
# Request validation and payloads
# ---------------------------------------------------------------------------

def validate_request(
    prompt,
    model,
    resolution,
    ratio,
    duration,
    first_frame,
    last_frame,
    reference_images,
    reference_videos,
    reference_audios,
    draft,
    draft_task_id,
    omni_reference_task_type,
    output_format,
):
    clean_model, spec = model_spec(model)
    family = spec["family"]
    duration = int(duration)
    reference_images = list(reference_images or [])
    reference_videos = list(reference_videos or [])
    reference_audios = list(reference_audios or [])

    if resolution not in spec["resolutions"]:
        raise RuntimeError(
            "%s 不支持 resolution=%s / %s supports: %s"
            % (clean_model, resolution, clean_model, ", ".join(spec["resolutions"]))
        )
    if ratio not in RATIOS:
        raise RuntimeError("ratio 不受支持 / unsupported ratio: %s" % ratio)
    if duration != -1 and not 4 <= duration <= spec["max_duration"]:
        raise RuntimeError(
            "%s 的 duration 只能是 -1 或 4 到 %d 秒 / duration must be -1 or 4-%d"
            % (clean_model, spec["max_duration"], spec["max_duration"])
        )
    if last_frame and not first_frame:
        raise RuntimeError("只给尾帧不够 / last_frame requires first_frame")

    has_content = bool(
        (prompt and prompt.strip())
        or first_frame
        or last_frame
        or reference_images
        or reference_videos
        or reference_audios
        or draft_task_id
    )
    if not has_content:
        raise RuntimeError(
            "至少要提供 prompt、首帧/尾帧、参考素材或 draft_task_id / provide prompt, frames, "
            "reference media, or draft_task_id"
        )

    if len(reference_images) > spec["max_images"]:
        raise RuntimeError(
            "reference_images 最多 %d 个 / at most %d reference images"
            % (spec["max_images"], spec["max_images"])
        )
    if len(reference_videos) > spec["max_videos"]:
        raise RuntimeError(
            "reference_videos 最多 %d 个 / at most %d reference videos"
            % (spec["max_videos"], spec["max_videos"])
        )
    if len(reference_audios) > spec["max_audios"]:
        raise RuntimeError(
            "reference_audios 最多 %d 个 / at most %d reference audios"
            % (spec["max_audios"], spec["max_audios"])
        )

    has_reference = bool(reference_images or reference_videos or reference_audios)
    if (first_frame or last_frame) and has_reference:
        raise RuntimeError(
            "关键帧模式与参考模式不能混用 / first/last frame cannot be mixed with reference media"
        )

    if family == "2.0":
        if draft or draft_task_id:
            raise RuntimeError("Draft 模式只支持 seedance-2.5 / Draft is only available on Seedance 2.5")
        if first_frame and ratio == "adaptive":
            raise RuntimeError(
                "当前 Vergora Seedance 2.0 网关不接受图生视频 ratio=adaptive；"
                "请选择 16:9、4:3、1:1、3:4、9:16 或 21:9"
                " / current Seedance 2.0 gateway rejects adaptive ratio for image-to-video"
            )
        if reference_audios and not (reference_images or reference_videos):
            raise RuntimeError(
                "Seedance 2.0 不支持纯音频或文本+音频 / Seedance 2.0 does not support audio-only "
                "or text+audio; add a reference image or video"
            )
        return clean_model

    if output_format not in OUTPUT_FORMATS:
        raise RuntimeError("output_format 只能是 mp4 或 mov / output_format must be mp4 or mov")
    if draft and draft_task_id:
        raise RuntimeError("draft 与 draft_task_id 不能同时使用 / cannot use draft and draft_task_id together")

    if draft_task_id:
        leftovers = bool(
            (prompt and prompt.strip())
            or first_frame
            or last_frame
            or reference_images
            or reference_videos
            or reference_audios
        )
        if leftovers:
            raise RuntimeError(
                "从 Draft 生成最终视频时不能再提供 prompt 或素材 / when draft_task_id is set, "
                "do not provide prompt or media"
            )
        if resolution != "1080p":
            raise RuntimeError(
                "从 Draft 生成最终视频目前只支持 1080p / final video from a Draft must use 1080p"
            )
        return clean_model

    if draft and resolution != "480p":
        raise RuntimeError("Draft 阶段固定使用 480p / Draft generation requires resolution=480p")

    if first_frame and ratio != "adaptive":
        raise RuntimeError(
            "Seedance 2.5 首帧/首尾帧任务只能用 ratio=adaptive / first/last frame requires adaptive"
        )

    if omni_reference_task_type not in OMNI_TASK_TYPES:
        raise RuntimeError(
            "omni_reference_task_type 只能是 auto/reference/edit/extend / unsupported task type"
        )
    if omni_reference_task_type in ("edit", "extend") and not reference_videos:
        raise RuntimeError(
            "编辑或扩展视频至少需要一个 reference video / edit and extend require a reference video"
        )
    if omni_reference_task_type == "edit" and (ratio != "adaptive" or duration != -1):
        raise RuntimeError(
            "Seedance 2.5 视频编辑要求 ratio=adaptive、duration=-1 / video editing requires "
            "ratio=adaptive and duration=-1"
        )
    if omni_reference_task_type == "extend" and ratio != "adaptive":
        raise RuntimeError("Seedance 2.5 视频扩展要求 ratio=adaptive / video extension requires adaptive")

    return clean_model


def _base_options(
    model,
    resolution,
    ratio,
    duration,
    generate_audio,
    watermark,
    return_last_frame,
    draft,
    omni_reference_task_type,
    output_format,
    draft_task_id,
):
    if draft_task_id:
        # The final-from-Draft request reuses prompt, media, ratio, duration,
        # audio and task type. Only these parameters may be supplied again.
        return {
            "model": model,
            "resolution": resolution,
            "output_format": output_format,
            "watermark": bool(watermark),
            "return_last_frame": bool(return_last_frame),
        }
    payload = {
        "model": model,
        "resolution": resolution,
        "generate_audio": bool(generate_audio),
        "watermark": bool(watermark),
        "return_last_frame": bool(return_last_frame),
    }
    payload["ratio"] = ratio
    payload["duration"] = int(duration)
    if MODEL_SPECS[model]["family"] == "2.5":
        payload["output_format"] = output_format
        payload["omni_reference_task_type"] = omni_reference_task_type
        if draft:
            payload["draft"] = True
    return payload


def _draft_task_content(draft_task_id):
    return [{"type": "draft_task", "draft_task": {"id": draft_task_id}}]


def _build_flat_payload(
    prompt,
    model,
    resolution,
    ratio,
    duration,
    generate_audio,
    watermark,
    return_last_frame,
    first_frame,
    last_frame,
    reference_images,
    reference_videos,
    reference_audios,
    draft,
    draft_task_id,
    omni_reference_task_type,
    output_format,
):
    payload = _base_options(
        model,
        resolution,
        ratio,
        duration,
        generate_audio,
        watermark,
        return_last_frame,
        draft,
        omni_reference_task_type,
        output_format,
        draft_task_id,
    )
    if draft_task_id:
        payload["content"] = _draft_task_content(draft_task_id)
        return payload

    if prompt and prompt.strip():
        payload["prompt"] = prompt.strip()
    if first_frame:
        payload["first_frame"] = first_frame
    if last_frame:
        payload["last_frame"] = last_frame
    if reference_images:
        payload["reference_images"] = list(reference_images)
    if reference_videos:
        payload["reference_videos"] = list(reference_videos)
    if reference_audios:
        payload["reference_audios"] = list(reference_audios)
    return payload


def _build_content_payload(
    prompt,
    model,
    resolution,
    ratio,
    duration,
    generate_audio,
    watermark,
    return_last_frame,
    first_frame,
    last_frame,
    reference_images,
    reference_videos,
    reference_audios,
    draft,
    draft_task_id,
    omni_reference_task_type,
    output_format,
):
    payload = _base_options(
        model,
        resolution,
        ratio,
        duration,
        generate_audio,
        watermark,
        return_last_frame,
        draft,
        omni_reference_task_type,
        output_format,
        draft_task_id,
    )
    if draft_task_id:
        payload["content"] = _draft_task_content(draft_task_id)
        return payload

    content = []
    if prompt and prompt.strip():
        content.append({"type": "text", "text": prompt.strip()})
    if first_frame:
        content.append({"type": "image_url", "role": "first_frame", "image_url": {"url": first_frame}})
    if last_frame:
        content.append({"type": "image_url", "role": "last_frame", "image_url": {"url": last_frame}})
    for uri in reference_images:
        content.append({"type": "image_url", "role": "reference_image", "image_url": {"url": uri}})
    for uri in reference_videos:
        content.append({"type": "video_url", "role": "reference_video", "video_url": {"url": uri}})
    for uri in reference_audios:
        content.append({"type": "audio_url", "role": "reference_audio", "audio_url": {"url": uri}})
    payload["content"] = content
    return payload


def build_payload(
    prompt,
    model,
    resolution,
    ratio,
    duration,
    generate_audio,
    watermark,
    return_last_frame,
    first_frame=None,
    last_frame=None,
    reference_images=None,
    reference_videos=None,
    reference_audios=None,
    draft=False,
    draft_task_id="",
    omni_reference_task_type="auto",
    output_format="mp4",
    extra_json="",
    request_format="flat",
):
    reference_images = list(reference_images or [])
    reference_videos = list(reference_videos or [])
    reference_audios = list(reference_audios or [])
    draft_task_id = (draft_task_id or "").strip()
    clean_model = validate_request(
        prompt,
        model,
        resolution,
        ratio,
        duration,
        first_frame,
        last_frame,
        reference_images,
        reference_videos,
        reference_audios,
        bool(draft),
        draft_task_id,
        omni_reference_task_type,
        output_format,
    )
    args = (
        prompt, clean_model, resolution, ratio, int(duration), generate_audio, watermark,
        return_last_frame, first_frame, last_frame,
        reference_images, reference_videos, reference_audios, bool(draft),
        draft_task_id, omni_reference_task_type, output_format,
    )
    if request_format == "content":
        payload = _build_content_payload(*args)
    elif request_format == "flat":
        payload = _build_flat_payload(*args)
    else:
        raise RuntimeError("未知 REQUEST_FORMAT / unknown REQUEST_FORMAT: %s" % request_format)

    if extra_json and extra_json.strip():
        try:
            extra = json.loads(extra_json)
        except Exception as exc:
            raise RuntimeError("extra_json 不是合法 JSON / invalid JSON: %s" % exc)
        if not isinstance(extra, dict):
            raise RuntimeError("extra_json 必须是 JSON 对象 / extra_json must be a JSON object")
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _dict_at(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _task_holder(data):
    for holder in (
        _dict_at(data, "task_info"),
        _dict_at(_dict_at(data, "data"), "task_info"),
        _dict_at(data, "data"),
        data,
    ):
        if holder:
            return holder
    return {}


def extract_task_id(data):
    holder = _task_holder(data)
    for value in (holder.get("id"), holder.get("task_id"), data.get("id") if isinstance(data, dict) else None):
        if value:
            return str(value)
    return None


def extract_status(data):
    holder = _task_holder(data)
    return str(holder.get("status") or data.get("status") or "").lower()


def extract_draft_flag(data):
    """Return the gateway's echoed draft flag, or None when it is omitted."""
    if not isinstance(data, dict):
        return None
    for holder in (
        _dict_at(data, "task_info"),
        _dict_at(data, "data"),
        data,
    ):
        if "draft" not in holder:
            continue
        value = holder.get("draft")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "false"):
                return lowered == "true"
    return None


def payload_has_reference_video(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("reference_videos"):
        return True
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict) and item.get("type") in ("video_url", "video")
        for item in content
    )


def payload_has_first_frame(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("first_frame"):
        return True
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "image_url"
        and item.get("role") == "first_frame"
        for item in content
    )


def extract_video_url(data):
    if not isinstance(data, dict):
        return None
    videos = data.get("videos")
    if isinstance(videos, list) and videos:
        first = videos[0]
        if isinstance(first, str) and first:
            return first
        if isinstance(first, dict):
            for key in ("url", "video_url", "file_url", "content_url"):
                if first.get(key):
                    return first[key]
    for holder in (data, _dict_at(data, "task_info"), _dict_at(data, "content"),
                   _dict_at(data, "output"), _dict_at(data, "result"), _dict_at(data, "data")):
        if not isinstance(holder, dict):
            continue
        for key in ("video_url", "url"):
            if isinstance(holder.get(key), str) and holder[key]:
                return holder[key]
    nested_content = _dict_at(_dict_at(data, "data"), "content")
    if nested_content.get("video_url"):
        return nested_content["video_url"]
    return None


def extract_last_frame_url(data):
    if not isinstance(data, dict):
        return None
    for holder in (
        data,
        _dict_at(data, "task_info"),
        _dict_at(data, "content"),
        _dict_at(data, "data"),
        _dict_at(_dict_at(data, "data"), "content"),
    ):
        if isinstance(holder, dict):
            for key in ("last_frame_url", "last_frame", "last_frame_image_url"):
                value = holder.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, dict) and value.get("url"):
                    return value["url"]
    return None


def extract_failure_reason(data):
    candidates = []
    top_error = data.get("error")
    if isinstance(top_error, dict):
        candidates.append(top_error)
    elif isinstance(top_error, str) and top_error.strip():
        candidates.append({"message": top_error})
    for holder in (data.get("task_info"), data.get("data"), data):
        if not isinstance(holder, dict):
            continue
        if isinstance(holder.get("error"), dict):
            candidates.append(holder["error"])
        fail = holder.get("fail_reason")
        if isinstance(fail, dict):
            candidates.append(fail)
        elif isinstance(fail, str) and fail.strip().startswith("{"):
            try:
                candidates.append(json.loads(fail))
            except Exception:
                candidates.append({"message": fail})
        elif isinstance(fail, str) and fail.strip():
            candidates.append({"message": fail})
        for key in ("message", "detail", "title", "code"):
            value = holder.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append({"code" if key == "code" else "message": value})
    for item in candidates:
        code = str(item.get("code") or "").strip()
        message = str(item.get("message") or item.get("detail") or item.get("title") or "").strip()
        if code or message:
            return code, message
    return "", ""


def explain_http_error(
    status,
    text,
    model=None,
    request_format=None,
    draft=False,
    task_type=None,
    has_reference_video=False,
    has_first_frame=False,
):
    lower = (text or "").lower()
    snippet = (text or "")[:300]
    if (
        status == 402
        or "insufficient balance" in lower
        or "insufficient_quota" in lower
        or "quota is not enough" in lower
    ):
        need = re.search(r"required:\s*\d+\s*amount\s*\(([0-9.]+)\s*credits?\)", lower)
        have = re.search(r"available:\s*\d+\s*amount\s*\(([0-9.]+)\s*credits?\)", lower)
        balance_detail = ""
        if need or have:
            balance_detail = " 需要 %s credits，可用 %s credits。" % (
                need.group(1) if need else "?",
                have.group(1) if have else "?",
            )
        return (
            "余额或配额不足 / insufficient balance or quota (HTTP %s). "
            "Key 本身可能有效，但账户余额、Key budget 或所选模型的可用额度不足。%s"
            " Upstream said: %s" % (status, balance_detail, snippet)
        )
    if status in (401, 403) and "api key" in lower:
        return (
            "鉴权失败 / authentication failed (HTTP %s): Vergora API key 无效、过期或不属于当前环境。"
            " Upstream said: %s" % (status, snippet)
        )
    if status in (401, 403):
        return "鉴权或权限失败 / authentication or permission failed (HTTP %s): %s" % (status, snippet)
    if status == 413:
        return (
            "请求体过大 / request body too large (HTTP 413). 本地图片会先压缩，但多张图片仍可能超过"
            "网关限制；请减少内嵌素材或改用公开素材链接。"
        )
    if status == 404:
        return (
            "接口返回 404 / route not found: 检查 BASE_URL、代理和 /v1/videos 路由。"
            " Upstream said: %s" % snippet
        )
    if status == 429:
        return "触发限流 / rate limited (HTTP 429). 稍后重试或降低并发。 Upstream said: %s" % snippet
    if status == 400:
        if "estimate_video_tokens_failed" in lower:
            if has_reference_video and task_type in ("edit", "extend"):
                return (
                    "当前 Vergora 网关无法提交 Seedance 2.5 视频编辑/扩展 / the gateway "
                    "cannot estimate an adaptive video-input task. 网关在提交阶段以 "
                    "`invalid video ratio` 拒绝 `adaptive`；改用固定画幅虽能建单，但上游会"
                    "再次报错并要求 `ratio=adaptive`。这是当前网关的视频 token 估算限制，"
                    "不是素材 URL 或 flat/content 格式问题，任务尚未创建。请暂时使用普通"
                    " Seedance 2.5 生成。 Upstream said: %s" % snippet
                )
            if has_first_frame:
                return (
                    "当前 Vergora 网关无法提交 Seedance 2.5 首帧/首尾帧任务 / the gateway "
                    "cannot estimate an adaptive image-input task. 网关以 `invalid video ratio` "
                    "拒绝 `adaptive`，但上游规定首帧/首尾帧只能使用 `adaptive`。因此这是"
                    "当前网关的 token 估算限制，不是首帧图片不可访问，任务尚未创建。请暂时"
                    "使用 Seedance 2.0 图生视频或普通 2.5 文生视频。 Upstream said: %s" % snippet
                )
            if request_format == "content":
                draft_detail = (
                    " 当前请求已经是 content[]，并且启用了 Draft；这不是 flat/content 格式问题。"
                    "官方 Draft 示例要求 480p、adaptive、MOV 和 draft=true，若按此仍失败，"
                    "说明当前 Vergora 网关没有正确支持 Draft 视频估算。"
                    if draft else
                    " 当前请求已经是 content[]，不能再通过切换请求格式修复。"
                )
                return (
                    "上游无法估算当前视频素材的 token，任务尚未创建 / the gateway could not "
                    "estimate the video input.%s 请检查素材 URL 是否可访问、视频时长和画幅参数；"
                    "不要在没有修改参数时反复提交。 Upstream said: %s"
                    % (draft_detail, snippet)
                )
            return (
                "上游无法估算当前视频素材的 token，任务尚未创建 / the gateway could not estimate "
                "the video input. 当前网关常见于 flat 模式提交 reference video；请设置 "
                "REQUEST_FORMAT = content 后重新运行。若刚改过网络或代理，先在 Requests "
                "确认没有已有记录。 Upstream said: %s" % snippet
            )
        extra = ""
        if any(word in lower for word in ("content", "unknown field", "additionalproperties")):
            extra = (
                " 如果错误明确说缺少 content 或字段未知，请在 config.ini 设置 REQUEST_FORMAT = content；"
                "若说 prompt/媒体字段未知，则设置 REQUEST_FORMAT = flat。"
            )
        return "上游拒绝了参数 / request rejected (HTTP 400): %s%s" % (snippet, extra)
    if status >= 500:
        if "no available channel" in lower:
            return (
                "网关当前没有该模型的可用通道 / no available channel (HTTP %s). "
                "这不是节点参数错误；通道恢复后再运行，或改用其他 Seedance 型号。"
                " Upstream said: %s" % (status, snippet)
            )
        return "上游服务错误 / upstream error (HTTP %s): %s" % (status, snippet)
    return None


def explain_task_failure(data, text):
    code, detail = extract_failure_reason(data)
    lower = ("%s %s" % (code, detail)).lower()
    if "draft_task is not a draft" in lower:
        return (
            "网关没有把来源任务登记为 Draft，因此不能生成最终视频 / the gateway did not mark "
            "the source task as a draft. 当前 Vergora 网关会把 draft=true 保存成 draft=false，"
            "并把第一阶段生成为普通 480p 视频；请暂时使用普通 Seedance 2.5 任务，不要重复提交"
            "第二阶段。 Upstream said: %s" % (detail or text[:400])
        )
    if "tasktypemismatch" in lower or "tasktypeconstraint" in lower or "task type" in lower:
        return (
            "任务类型与参数不一致 / task type mismatch. Seedance 2.5 编辑任务要设置 "
            "omni_reference_task_type=edit、ratio=adaptive、duration=-1，并使用明确的编辑动词；"
            "扩展任务设置 extend 和 adaptive；首帧/首尾帧任务只能用 adaptive。"
            " Upstream said: %s" % (detail or text[:400])
        )
    if "failed to download" in lower or "download file" in lower:
        return (
            "任务失败：上游下载不到素材链接 / the upstream could not fetch a media URL. "
            "请改用可长期公开访问的链接。 Upstream said: %s" % (detail or text[:400])
        )
    if "resolution" in lower:
        return (
            "任务失败：模型不支持这个分辨率 / unsupported resolution. "
            " Upstream said: %s" % (detail or text[:400])
        )
    return detail or text[:600]


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------

def _preview_payload(payload):
    def clean(value):
        if isinstance(value, str):
            if value.startswith("data:"):
                return "data-uri(%d chars)" % len(value)
            return value if len(value) <= 100 else value[:97] + "..."
        if isinstance(value, list):
            return [clean(item) for item in value[:20]]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value
    return clean(payload)


def submit_task(cfg, payload, request_format=None):
    url = cfg["base_url"] + "/videos"
    log("[vergora-seedance] 提交 / submit -> %s  %s"
        % (url, json.dumps(_preview_payload(payload), ensure_ascii=False)))
    try:
        status, text = http_post_json(
            url,
            auth_headers(cfg),
            payload,
            timeout=float(cfg.get("submit_timeout", DEFAULT_SUBMIT_TIMEOUT)),
        )
    except Exception as exc:
        if looks_like_network_error(exc):
            raise RuntimeError(network_error_message(exc, url)) from exc
        raise
    if status >= 400:
        if request_format is None:
            request_format = "content" if isinstance(payload.get("content"), list) else "flat"
        hint = explain_http_error(
            status,
            text,
            payload.get("model"),
            request_format=request_format,
            draft=bool(payload.get("draft")),
            task_type=payload.get("omni_reference_task_type"),
            has_reference_video=payload_has_reference_video(payload),
            has_first_frame=payload_has_first_frame(payload),
        )
        message = hint or "提交失败 / submit failed HTTP %s: %s" % (status, text[:500])
        if status in (400, 422) and looks_like_schema_mismatch(text):
            raise SubmitSchemaError(message)
        raise RuntimeError(message)
    try:
        data = json.loads(text)
    except Exception as exc:
        raise RuntimeError("提交返回不是 JSON / response is not JSON: %s" % text[:500]) from exc
    task_id = extract_task_id(data)
    if not task_id:
        raise RuntimeError("返回里没有任务 id / no task id in response: %s" % text[:500])
    log("[vergora-seedance] 任务已创建 / task created id=%s" % task_id)
    return task_id


def wait_for_video(
    cfg,
    task_id,
    progress=None,
    expect_draft=False,
    expected_output_format=None,
):
    started = time.time()
    consecutive_errors = 0
    format_warning_logged = False
    while True:
        throw_exception_if_processing_interrupted()
        elapsed = time.time() - started
        tick_progress(progress, min(95, int(elapsed / max(1.0, cfg["timeout"]) * 95)))
        if elapsed > cfg["timeout"]:
            raise RuntimeError(
                "等待超时 / timed out after %.0f s. Task %s may still be running: GET %s/videos/%s"
                % (cfg["timeout"], task_id, cfg["base_url"], task_id)
            )
        try:
            status, text = http_get_json(
                cfg["base_url"] + "/videos/" + task_id,
                auth_headers(cfg, with_json=False),
                timeout=60,
            )
            if status >= 400:
                raise RuntimeError(explain_http_error(status, text)
                                   or "HTTP %s: %s" % (status, text[:300]))
            data = json.loads(text)
        except Exception as exc:
            consecutive_errors += 1
            log("[vergora-seedance] 查询失败第 %d 次 / poll error %d: %s"
                % (consecutive_errors, consecutive_errors, exc))
            if consecutive_errors >= 5:
                raise RuntimeError("连续 5 次查询失败 / 5 consecutive poll failures: %s" % exc)
            time.sleep(cfg["poll_interval"])
            continue
        consecutive_errors = 0
        state = extract_status(data)
        log("[vergora-seedance] 已等待 / waited %4.0f s  status=%s" % (elapsed, state or "unknown"))
        if expect_draft and extract_draft_flag(data) is False:
            raise RuntimeError(
                "网关没有把任务登记为 Draft / the gateway stored this task with draft=false "
                "(task %s). 当前 Vergora 网关会丢弃 draft=true，并把第一阶段生成为普通 480p "
                "视频；它不能作为 draft_task_id 生成最终视频。节点已停止等待，也不会把该视频"
                "静默当作 Draft。请暂时使用普通 Seedance 2.5 任务，不要立即重复提交。" % task_id
            )
        actual_format = str(_task_holder(data).get("output_format") or "").lower()
        if (
            expected_output_format
            and actual_format
            and actual_format != str(expected_output_format).lower()
            and not format_warning_logged
        ):
            log(
                "[vergora-seedance] 网关把输出格式 %s 改成了 %s / gateway changed output_format "
                "to %s; the downloaded file will use the actual format"
                % (expected_output_format, actual_format, actual_format)
            )
            format_warning_logged = True
        if state in ("completed", "succeeded", "success", "done"):
            video_url = extract_video_url(data)
            if not video_url:
                raise RuntimeError("任务已完成但没有视频地址 / completed without video URL: %s" % text[:500])
            return video_url, extract_last_frame_url(data)
        if state in ("failed", "error", "canceled", "cancelled", "expired"):
            raise RuntimeError("任务失败 / task failed: %s" % explain_task_failure(data, text))
        time.sleep(cfg["poll_interval"])


def resolve_output_dir(cfg):
    if cfg["output_dir"]:
        return cfg["output_dir"]
    if folder_paths is not None:
        try:
            return folder_paths.get_output_directory()
        except Exception:
            pass
    return os.path.join(HERE, "output")


def download_asset(url, path, minimum_bytes, attempts=3):
    part = path + ".part"
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            http_download(url, part, timeout=600)
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if size >= minimum_bytes:
                os.replace(part, path)
                log("[vergora-seedance] 已保存 / saved %s (%.1f MB)"
                    % (path, size / 1024.0 / 1024.0))
                return path
            last_error = "只收到 %d 字节 / got only %d bytes" % (size, size)
        except Exception as exc:
            last_error = str(exc)
        log("[vergora-seedance] 下载不完整，第 %d/%d 次重试 / incomplete download, retry %d/%d: %s"
            % (attempt, attempts, attempt, attempts, last_error))
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        time.sleep(2 * attempt)
    raise RuntimeError(
        "下载失败（%d 次）/ download failed after %d attempts: %s -- asset: %s"
        % (attempts, attempts, last_error, url)
    )


def download_video(cfg, video_url, task_id, output_format):
    ext = ".mov" if str(output_format).lower() == "mov" else ".mp4"
    if not video_url.lower().split("?", 1)[0].endswith(ext):
        guessed = os.path.splitext(video_url.split("?", 1)[0])[1].lower()
        if guessed in (".mp4", ".mov"):
            ext = guessed
    out_dir = resolve_output_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    filename = "vergora_seedance_%s_%d%s" % (
        str(task_id).replace("-", "")[:8], int(time.time()), ext
    )
    return download_asset(video_url, os.path.join(out_dir, filename), MIN_VIDEO_BYTES)


def download_last_frame(cfg, frame_url, task_id):
    path_part = frame_url.split("?", 1)[0].lower()
    ext = os.path.splitext(path_part)[1]
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".jpg"
    out_dir = resolve_output_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    filename = "vergora_seedance_%s_last_%d%s" % (
        str(task_id).replace("-", "")[:8], int(time.time()), ext
    )
    return download_asset(frame_url, os.path.join(out_dir, filename), MIN_IMAGE_BYTES)


def build_preview_ui(path):
    if folder_paths is None:
        return None
    try:
        out_root = os.path.abspath(folder_paths.get_output_directory())
    except Exception:
        return None
    path = os.path.abspath(path)
    if not path.startswith(out_root + os.sep):
        return None
    relative = os.path.relpath(path, out_root)
    subfolder = os.path.dirname(relative).replace(os.sep, "/")
    return {
        "filename": os.path.basename(relative),
        "subfolder": "" if subfolder == "." else subfolder,
        "type": "output",
        "format": mimetypes.guess_type(path)[0] or "video/mp4",
    }


def generate(
    prompt,
    model,
    resolution,
    ratio,
    duration,
    generate_audio,
    watermark,
    return_last_frame,
    first_frame=None,
    last_frame=None,
    first_frame_url="",
    last_frame_url="",
    reference_images=None,
    reference_image_urls="",
    reference_video_urls="",
    reference_audio_urls="",
    draft=False,
    draft_task_id="",
    omni_reference_task_type="auto",
    output_format="mp4",
    extra_json="",
):
    cfg = load_config()
    progress = make_progress()
    total_frames = (
        count_frames(first_frame) + count_frames(last_frame) + count_frames(reference_images)
    )
    budget = max(48 * 1024, MAX_BODY_IMAGE_BYTES // max(1, total_frames))
    frame = tensor_to_data_uri(first_frame, budget) or (first_frame_url or "").strip() or None
    tail = tensor_to_data_uri(last_frame, budget) or (last_frame_url or "").strip() or None

    clean_model, spec = model_spec(model)
    images = tensor_batch_to_data_uris(reference_images, spec["max_images"], budget)
    images += split_uri_lines(reference_image_urls)
    videos = split_uri_lines(reference_video_urls)
    audios = split_uri_lines(reference_audio_urls)

    requested_format = cfg["request_format"]
    effective_format = "flat" if requested_format == "auto" else requested_format
    payload_args = (
        prompt,
        clean_model,
        resolution,
        ratio,
        duration,
        generate_audio,
        watermark,
        return_last_frame,
        frame,
        tail,
        images,
        videos,
        audios,
    )
    payload_kwargs = {
        "draft": draft,
        "draft_task_id": draft_task_id,
        "omni_reference_task_type": omni_reference_task_type,
        "output_format": output_format,
        "extra_json": extra_json,
        "request_format": effective_format,
    }
    payload = build_payload(*payload_args, **payload_kwargs)
    tick_progress(progress, 3)
    try:
        request_format = payload_kwargs["request_format"]
        task_id = submit_task(cfg, payload, request_format=request_format)
    except SubmitSchemaError:
        if requested_format != "auto":
            raise
        # A schema rejection does not create a task. Retry only this explicit,
        # non-billable mismatch; never retry ambiguous 4xx/5xx submit errors.
        log("[vergora-seedance] 网关不接受 flat 字段，自动改用 content[] / "
            "gateway rejected flat fields, retrying with content[]")
        payload_kwargs["request_format"] = "content"
        payload = build_payload(*payload_args, **payload_kwargs)
        request_format = payload_kwargs["request_format"]
        task_id = submit_task(cfg, payload, request_format=request_format)
    video_url, last_frame_url = wait_for_video(
        cfg,
        task_id,
        progress,
        expect_draft=bool(draft),
        expected_output_format=output_format if clean_model == "seedance-2.5" else None,
    )
    video_path = download_video(cfg, video_url, task_id, output_format)

    last_frame_path = ""
    if last_frame_url:
        try:
            last_frame_path = download_last_frame(cfg, last_frame_url, task_id)
        except Exception as exc:
            log("[vergora-seedance] 尾帧下载失败，但视频已保存 / last frame download failed: %s" % exc)
    tick_progress(progress, 100)
    return video_path, task_id, video_url, last_frame_path


# ---------------------------------------------------------------------------
# ComfyUI nodes
# ---------------------------------------------------------------------------

def _common_required(models, default_model, resolutions, max_duration):
    return {
        "prompt": ("STRING", {"multiline": True, "default": ""}),
        "model": (models, {"default": default_model}),
        "resolution": (resolutions, {"default": "720p"}),
        "ratio": (RATIOS, {"default": "adaptive"}),
        "duration": (
            "INT",
            {
                "default": 5,
                "min": -1,
                "max": max_duration,
                "step": 1,
                "tooltip": "-1 = automatic duration; otherwise 4 to %d seconds" % max_duration,
            },
        ),
        "generate_audio": ("BOOLEAN", {"default": True}),
        "watermark": ("BOOLEAN", {"default": False}),
        "return_last_frame": ("BOOLEAN", {"default": False}),
    }


def _common_optional():
    return {
        "first_frame": ("IMAGE", {"tooltip": "Image-to-video first frame"}),
        "last_frame": ("IMAGE", {"tooltip": "Optional last frame; requires first_frame"}),
        "first_frame_url": ("STRING", {"default": "", "tooltip": "Used when first_frame is not connected"}),
        "last_frame_url": ("STRING", {"default": "", "tooltip": "Used when last_frame is not connected"}),
        "reference_images": ("IMAGE", {"tooltip": "Reference image batch; do not mix with keyframes"}),
        "reference_image_urls": (
            "STRING",
            {"multiline": True, "default": "", "tooltip": "Reference image URLs, one per line"},
        ),
        "reference_video_urls": (
            "STRING",
            {"multiline": True, "default": "", "tooltip": "Reference video URLs, one per line"},
        ),
        "reference_audio_urls": (
            "STRING",
            {"multiline": True, "default": "", "tooltip": "Reference audio URLs, one per line"},
        ),
        "extra_json": (
            "STRING",
            {
                "multiline": True,
                "default": "",
                "tooltip": 'Extra request fields, for example {"service_tier":"default"}',
            },
        ),
    }


class VergoraSeedance20:
    """Seedance 2.0, 2.0 fast and 2.0 mini."""

    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("video_path", "task_id", "video_url", "last_frame_path")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": _common_required(
                SEEDANCE_20_MODELS, "seedance-2.0", ["4k", "1080p", "720p", "480p"], 15
            ),
            "optional": _common_optional(),
        }

    def run(
        self,
        prompt,
        model,
        resolution,
        ratio,
        duration,
        generate_audio,
        watermark,
        return_last_frame,
        first_frame=None,
        last_frame=None,
        first_frame_url="",
        last_frame_url="",
        reference_images=None,
        reference_image_urls="",
        reference_video_urls="",
        reference_audio_urls="",
        extra_json="",
    ):
        result = generate(
            prompt, model, resolution, ratio, duration, generate_audio, watermark,
            return_last_frame, first_frame, last_frame, first_frame_url,
            last_frame_url, reference_images, reference_image_urls, reference_video_urls,
            reference_audio_urls, False, "", "auto", "mp4", extra_json,
        )
        preview = build_preview_ui(result[0])
        if preview is None:
            return {"result": result}
        return {"ui": {"images": [preview], "animated": (True,)}, "result": result}


class VergoraSeedance25:
    """Seedance 2.5, including omni reference, edit/extend and Draft mode."""

    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("video_path", "task_id", "video_url", "last_frame_path")
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        required = _common_required(
            SEEDANCE_25_MODELS, SEEDANCE_25_MODEL, ["1080p", "720p", "480p"], 30
        )
        required.update({
            "output_format": (OUTPUT_FORMATS, {"default": "mp4"}),
            "omni_reference_task_type": (
                OMNI_TASK_TYPES,
                {
                    "default": "auto",
                    "tooltip": "Use edit/extend with a reference video; validation happens locally",
                },
            ),
            "draft": (
                "BOOLEAN",
                {"default": False, "tooltip": "Draft generation is fixed at 480p"},
            ),
        })
        optional = _common_optional()
        optional["draft_task_id"] = (
            "STRING",
            {
                "default": "",
                "tooltip": "Final pass from an earlier seedance-2.5 Draft task; use 1080p",
            },
        )
        return {"required": required, "optional": optional}

    def run(
        self,
        prompt,
        model,
        resolution,
        ratio,
        duration,
        generate_audio,
        watermark,
        return_last_frame,
        output_format,
        omni_reference_task_type,
        draft,
        first_frame=None,
        last_frame=None,
        first_frame_url="",
        last_frame_url="",
        reference_images=None,
        reference_image_urls="",
        reference_video_urls="",
        reference_audio_urls="",
        extra_json="",
        draft_task_id="",
    ):
        if normalize_model(model) != SEEDANCE_25_MODEL:
            raise RuntimeError(
                "此节点只接受 %s / this node only accepts %s"
                % (SEEDANCE_25_MODEL, SEEDANCE_25_MODEL)
            )
        result = generate(
            prompt, SEEDANCE_25_MODEL, resolution, ratio, duration,
            generate_audio, watermark,
            return_last_frame, first_frame, last_frame, first_frame_url,
            last_frame_url, reference_images, reference_image_urls, reference_video_urls,
            reference_audio_urls, draft, draft_task_id, omni_reference_task_type,
            output_format, extra_json,
        )
        preview = build_preview_ui(result[0])
        if preview is None:
            return {"result": result}
        return {"ui": {"images": [preview], "animated": (True,)}, "result": result}


NODE_CLASS_MAPPINGS = {
    "VergoraSeedance25": VergoraSeedance25,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VergoraSeedance25": "vergora Seedance 2.5 视频生成（30秒/参考/编辑/草稿）",
}
