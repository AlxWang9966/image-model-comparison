# chinese-bookstore-poster

| Field | Value |
| --- | --- |
| Experiment | `28fd876eaa65452382359f4666eea9e3` |
| Created | 2026-09-10T08:04:19.702+00:00 |
| Source / status | live / completed |
| Requested size | 1024x1024 |
| Execution | parallel, 1 round(s) |
| Timing scope | `end_to_end` |
| Images retained | 3 / 3 samples |

## Full prompt

    设计一张中文海报。主标题：‘春日读书会’。副标题：‘周末城市书展’。画面是明亮的书店橱窗、柔和阳光、绿色植物、桌上的书和咖啡，版式简洁高级，中文标题清晰居中，温暖自然的商业海报风格。

## Images and per-image notes

| Model | Version | Round | Status | Image | Time (s) | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| MAI Image 2.5 | 2026-06-02 | 1 | success | [Original image](mai-image-2-5/round-01_mai-image-2-5_chinese-bookstore.png) | 26.388 | [JSON notes](mai-image-2-5/round-01_mai-image-2-5_chinese-bookstore.json) |
| GPT Image 2 | 2026-04-21 | 1 | success | [Original image](gpt-image-2/round-01_gpt-image-2_chinese-bookstore.png) | 65.852 | [JSON notes](gpt-image-2/round-01_gpt-image-2_chinese-bookstore.json) |
| FLUX.1 Kontext Pro | 1 | 1 | success | [Original image](flux-1-kontext-pro/round-01_flux-1-kontext-pro_chinese-bookstore.png) | 9.720 | [JSON notes](flux-1-kontext-pro/round-01_flux-1-kontext-pro_chinese-bookstore.json) |

## Interpretation and sharing

- Each image is a byte-for-byte copy of its retained source; SHA-256 is recorded in its JSON notes.
- End-to-end latency includes generation, network, decoding, and original image saving, not this secondary archive copy.
- Legacy API-response timing excludes decoding/saving. Do not pool the two timing scopes.
- A small sample is a demonstration, not a reliable quality/speed ranking.
- Missing legacy images and failed calls remain explicit; no synthetic replacement is created.
- JSON notes contain the full prompt, model, topic, parameters, timing, and any included human review.
- Connection endpoints, deployment aliases, subscription IDs, credentials, full errors, and absolute source paths are not included.
- Images, prompts, topics, and human notes may still contain private data. Review before sharing.
