# ComfyUI Vergora Seedance 2.5

ComfyUI custom node for **Seedance 2.5** through the Vergora API. This package
exports only `VergoraSeedance25`; Seedance 2.0 / Fast / Mini are installed from a
separate custom node folder so ComfyUI never mixes incompatible parameter sets.

## Features

- Text-to-video
- First-frame and first/last-frame generation
- Up to 30 reference images, 10 videos, and 10 audios
- `omni_reference_task_type`: `auto`, `reference`, `edit`, `extend`
- MOV request parameter and latency-free MP4 fallback naming
- Seedance 2.5 Draft stage and final-from-Draft workflow
- Flat gateway fields and native BytePlus `content[]`
- Asynchronous polling, interruption, verified download, and ComfyUI video preview
- API key stored only in `config.ini` or `VERGORA_API_KEY`

## Model

| Vergora Model ID | Resolution | Duration | References |
| --- | --- | --- | --- |
| `seedance-2.5` | 480p, 720p, 1080p | 4-30s or `-1` | 30 images + 10 videos + 10 audios |

## Inputs

| Input | Notes |
| --- | --- |
| `prompt` | Refer to assets as `Image 1`, `Video 1`, `Audio 1` |
| `model` | Always `seedance-2.5` |
| `resolution` | `480p`, `720p`, or `1080p` |
| `ratio` | `adaptive`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, `21:9` |
| `duration` | `-1`, or 4-30 seconds |
| `generate_audio` | Enable generated audio |
| `watermark` | Add the service watermark |
| `return_last_frame` | Download the last frame when returned |
| `output_format` | `mp4` or `mov` |
| `omni_reference_task_type` | `auto`, `reference`, `edit`, or `extend` |
| `draft` | Draft stage; requires 480p |
| `draft_task_id` | Generate the final 1080p video from a Draft task |
| `first_frame` / `last_frame` | Image tensors, or use the URL inputs |
| `reference_images` | ComfyUI image batch |
| `reference_image_urls` | One image URL/data URI per line |
| `reference_video_urls` | One video URL per line |
| `reference_audio_urls` | One audio URL per line |
| `extra_json` | Extra request fields, merged last |

## Validation

- First-frame and first-last-frame tasks require `ratio=adaptive`.
- `edit` requires a reference video, `ratio=adaptive`, and `duration=-1`.
- `extend` requires a reference video and `ratio=adaptive`.
- Draft generation requires `480p`.
- Final-from-Draft requires `1080p` and no prompt or media fields.
- Keyframe mode and reference mode cannot be mixed.
- A last frame requires a first frame.

## Current Vergora gateway compatibility

Live-tested on 2026-11-14 through `POST/GET /v1/videos`:

| Capability | Current status |
| --- | --- |
| Text and reference-image generation | Working |
| MOV output | Gateway stores `output_format=mp4` |
| Draft | Gateway stores `draft=false`; final pass is rejected |
| First/last frame | Blocked by adaptive image-input token estimation |
| Edit/extend | Blocked by adaptive video-input token estimation |

The node detects these gateway mismatches. A Draft request that comes back with
`draft=false` is stopped instead of being saved as an ordinary 480p video.
Requested MOV output is saved using the actual returned extension with a warning.

## Installation

1. Put this folder in ComfyUI's `custom_nodes` directory as
   `ComfyUI-Vergora-Seedance-2.5`.
2. Copy `config.ini.example` to `config.ini`.
3. Set `KEY` to a Vergora API key.
4. Restart ComfyUI.

Keep the two adapters in separate folders:

```text
custom_nodes/
  ComfyUI-Vergora-Seedance-2.0/
  ComfyUI-Vergora-Seedance-2.5/
```

Environment variables override `config.ini`:

```text
VERGORA_API_KEY
VERGORA_BASE_URL
VERGORA_REQUEST_FORMAT
```

## Request format

`REQUEST_FORMAT=auto` tries flat fields first and retries once with `content[]`
only when the gateway explicitly reports a missing/unknown schema field. Other
submit errors are never retried, avoiding duplicate tasks and billing.

## Development

```bash
python test_offline.py
```

The offline suite uses fake HTTP functions and creates no billed tasks.

## License

MIT. See `LICENSE`.

---

# 中文说明

这是独立的 **Seedance 2.5** ComfyUI 适配器，只注册 `VergoraSeedance25`。
2.0 / Fast / Mini 使用另一个独立目录，ComfyUI 不会在一个节点里混用两套模型参数。

2.5 支持 4-30 秒、30 图 + 10 视频 + 10 音频、MOV 参数、参考任务类型和 Draft
两阶段流程。首帧/首尾帧、Edit、Extend、Draft 的本地参数检查会在提交前完成。

当前网关实测：

- 2.5 文生和参考图：可用
- MOV：网关实际返回 MP4，节点按真实格式保存
- Draft：网关保存成 `draft=false`，节点会识别并停止，不再误当 Draft
- 首帧/首尾帧：被 adaptive 图片 token 估算问题阻断
- Edit/Extend：被 adaptive 视频 token 估算问题阻断

安装目录请保持独立：

```text
custom_nodes/
  ComfyUI-Vergora-Seedance-2.0/
  ComfyUI-Vergora-Seedance-2.5/
```

API Key 只从实例中的 `config.ini` 或 `VERGORA_API_KEY` 读取，不会保存到工作流。
