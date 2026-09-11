# flux-language-check-zh

| Field | Value |
| --- | --- |
| Experiment | `e45b7d7f86d8472199795f12bf0b4d0e` |
| Created | 2026-09-11T05:19:27.068+00:00 |
| Source / status | live / completed |
| Requested size | 1024x1024 |
| Execution | parallel, 1 round(s) |
| Timing scope | `end_to_end` |
| Images retained | 3 / 3 samples |

## Full prompt

    白色摄影棚背景，桌面上只有三个物体：画面左侧是一个红色立方体，右侧是一个蓝色球体，中间是一个带手柄的黄色马克杯。全部物体完整可见，真实产品摄影，柔和阴影，不要文字或装饰。

## Images and per-image notes

| Model | Version | Round | Prompt variant | Status | Image | Time (s) | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FLUX.1 Kontext Pro | 1 | 1 | original | success | [Original image](flux-1-kontext-pro/round-01_flux-1-kontext-pro_flux-language-chec.png) | 10.192 | [JSON notes](flux-1-kontext-pro/round-01_flux-1-kontext-pro_flux-language-chec.json) |
| FLUX.2 Pro | 1 | 1 | original | success | [Original image](flux-2-pro/round-01_flux-2-pro_flux-language-chec.png) | 11.211 | [JSON notes](flux-2-pro/round-01_flux-2-pro_flux-language-chec.json) |
| FLUX.2 Flex | 1 | 1 | original | success | [Original image](flux-2-flex/round-01_flux-2-flex_flux-language-chec.png) | 15.990 | [JSON notes](flux-2-flex/round-01_flux-2-flex_flux-language-chec.json) |

## Interpretation and sharing

- Each image is a byte-for-byte copy of its retained source; SHA-256 is recorded in its JSON notes.
- End-to-end latency includes generation, network, decoding, and original image saving, not this secondary archive copy.
- English translation/preparation is measured separately and excluded from image-generation latency.
- Per-image notes preserve original and effective prompts. Different language variants are not identical-prompt comparisons.
- Legacy API-response timing excludes decoding/saving. Do not pool the two timing scopes.
- A small sample is a demonstration, not a reliable quality/speed ranking.
- Missing legacy images and failed calls remain explicit; no synthetic replacement is created.
- JSON notes contain the full prompt, model, topic, parameters, timing, and any included human review.
- Connection endpoints, deployment aliases, subscription IDs, credentials, full errors, and absolute source paths are not included.
- Images, prompts, topics, and human notes may still contain private data. Review before sharing.
