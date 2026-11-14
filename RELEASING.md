# Releasing to the Comfy Registry

This package is published under the registry publisher `levi-li`.

## One-time setup

1. Open <https://registry.comfy.org> and sign in to the `levi-li` publisher.
2. Create a Registry Publishing API key.
3. Keep the key outside this repository. Never put it in `pyproject.toml`,
   `config.ini`, Git, or a workflow file.

## Before each release

1. Bump `version` in `pyproject.toml` using semantic versioning.
2. Add the matching release notes to `CHANGELOG.md`.
3. Run the offline tests: `python test_offline.py`.
4. Run `comfy node validate` and fix every error.
5. Commit and push the release changes.

## Publish

```bash
comfy node publish --changelog-file CHANGELOG.md
```

Enter the Registry Publishing API key when prompted. A published version is
immutable; publish a new semantic version instead of trying to replace it.

Registry page:
<https://registry.comfy.org/levi-li/comfyui-vergora-seedance-2-5>
