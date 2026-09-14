# GPT Image 2 / Flare / Sunburst 同轮对比

这是一次真实、独立的中文书店海报实验。三个模型同轮并行，使用相同原始中文提示词、`1024 × 1024` 尺寸和 `medium` 质量，每个模型生成 1 张。**未使用英文翻译，也没有拼接其他实验结果。**

| 项目 | 内容 |
| --- | --- |
| 主题 | `gpt-2-vs-2.5-bookstore` |
| 批次 | `6870d168a34e449eb37af098e2ec6816` |
| 创建时间 | 2026-09-11 08:39:17 UTC |
| 计时口径 | `end_to_end`，包含网络、图片传输、解码和原图保存 |
| 图片数量 | 3 / 3，实际返回均为 1024 × 1024 PNG |

## 完整提示词

> 设计一张中文海报。主标题：‘春日读书会’。副标题：‘周末城市书展’。画面是明亮的书店橱窗、柔和阳光、绿色植物、桌上的书和咖啡，版式简洁高级，中文标题清晰居中，温暖自然的商业海报风格。

## 原图与逐图备注

| 模型 | 模型版本 | 本次耗时 | 原图 | 完整备注 |
| --- | --- | --- | --- | --- |
| GPT Image 2 | 2026-04-21 | 64.730 s | [PNG](gpt-image-2/round-01_gpt-image-2_gpt-2-vs-2.5-books.png) | [JSON](gpt-image-2/round-01_gpt-image-2_gpt-2-vs-2.5-books.json) |
| GPT Image 2.5 Flare | 2026-09-08 | 23.644 s | [PNG](gpt-image-2-5/round-01_gpt-image-2-5_gpt-2-vs-2.5-books.png) | [JSON](gpt-image-2-5/round-01_gpt-image-2-5_gpt-2-vs-2.5-books.json) |
| GPT Image 2.5 Sunburst | 2026-09-08 | 40.642 s | [PNG](gpt-image-2-5-sunburst/round-01_gpt-image-2-5-sunburst_gpt-2-vs-2.5-books.png) | [JSON](gpt-image-2-5-sunburst/round-01_gpt-image-2-5-sunburst_gpt-2-vs-2.5-books.json) |

[批次 manifest](manifest.json) 汇总三个样本；每张图的备注包含完整提示词、实际发送版本、模型、尺寸、请求参数、分项耗时和 SHA-256。Flare 沿用兼容性的内部槽位 `gpt-image-2-5`，备注名称明确标识 Flare，不与 Sunburst 合并。

## 使用边界与隐私

这里只能说明这一次任务和环境的结果，**不是普遍速度排名、质量排行榜或服务性能承诺**。每个模型仅一个样本，没有自动评分或未经统一标准的人评分数。比较质量时可重点观察标题字形、额外文字、构图、材质与提示词遵循，并自行增加提示词和轮次。

图片为 AI 生成的原始输出，未修饰，公开前已检查无嵌入元数据。提示词是通用场景；分享包移除了 endpoint、订阅 / 资源信息、部署别名、完整错误、绝对源路径和私人评语。未来客户图片、提示词和备注仍需单独审核，不能因代码公开而自动公开业务数据。
