# 公开示例：中文书店海报

这个目录只包含**人工选择并审核后公开的一组示例**，不是维护者的全部历史，也不会自动接收未来生成的客户图片。

所有图片均为 AI 生成，按原始字节保留，未重新绘制或修饰。示例输入是通用的书店海报主题，不包含客户资料、个人身份信息或凭据。连接 endpoint、Azure 订阅 / 资源信息、部署别名及人工评语不在此分享包中，图片也已检查无嵌入元数据。

完整记录在 [批次说明](chinese-bookstore-poster/README.md) 和 [manifest.json](chinese-bookstore-poster/manifest.json)。每张原图旁都有单独的 JSON 备注，包含模型名称、主题、完整提示词、参数、耗时、尺寸和 SHA-256。

## 提示词

> 设计一张中文海报。主标题：‘春日读书会’。副标题：‘周末城市书展’。画面是明亮的书店橱窗、柔和阳光、绿色植物、桌上的书和咖啡，版式简洁高级，中文标题清晰居中，温暖自然的商业海报风格。

主题：`chinese-bookstore-poster`。三个模型各执行 1 次，同轮并行，目标与实际返回尺寸均为 `1024 × 1024`。GPT 的质量为 `medium`；MAI、FLUX 使用各自原生设置，并不是统一的“medium”质量档位。

## 原图与备注

| 模型 | 原图预览 | 逐图备注 | 本次端到端耗时 |
| --- | --- | --- | --- |
| MAI Image 2.5 | [原图](chinese-bookstore-poster/mai-image-2-5/round-01_mai-image-2-5_chinese-bookstore.png) | [JSON](chinese-bookstore-poster/mai-image-2-5/round-01_mai-image-2-5_chinese-bookstore.json) | 26.388 s |
| GPT Image 2 | [原图](chinese-bookstore-poster/gpt-image-2/round-01_gpt-image-2_chinese-bookstore.png) | [JSON](chinese-bookstore-poster/gpt-image-2/round-01_gpt-image-2_chinese-bookstore.json) | 65.852 s |
| FLUX.1 Kontext Pro | [原图](chinese-bookstore-poster/flux-1-kontext-pro/round-01_flux-1-kontext-pro_chinese-bookstore.png) | [JSON](chinese-bookstore-poster/flux-1-kontext-pro/round-01_flux-1-kontext-pro_chinese-bookstore.json) | 9.720 s |

### MAI Image 2.5

![MAI Image 2.5 中文书店海报](chinese-bookstore-poster/mai-image-2-5/round-01_mai-image-2-5_chinese-bookstore.png)

### GPT Image 2

![GPT Image 2 中文书店海报](chinese-bookstore-poster/gpt-image-2/round-01_gpt-image-2_chinese-bookstore.png)

### FLUX.1 Kontext Pro

![FLUX.1 Kontext Pro 对同一提示词的原始输出](chinese-bookstore-poster/flux-1-kontext-pro/round-01_flux-1-kontext-pro_chinese-bookstore.png)

## 如何解读

**这不是模型排行榜、服务承诺或采购结论。** 每个模型只有一个样本，时间包含当时的网络传输和原图保存，不是纯推理时间，也不代表你所在环境的表现。

本例 FLUX 的原始输出偏向山水画，没有呈现提示词要求的书店海报和文字。保留这一结果，是为了说明“更快”与“更符合任务要求”必须分开评价；不能用单张图推断模型对所有任务的质量。建议自行增加提示词和轮次，采用一致的人评标准。

没有 GPT Image 2.5 图片，因为该槽位未配置实际部署；不使用其他模型冒充。公开示例不包含人工评分或私人评语，避免把未经统一标准的人评数字呈现为客观质量指标。

## 分享自己的结果

自己的完整结果默认在本地 `image-lab-output` 和 `image-lab-archive`，不在这个目录。公开新案例前，请审核图片、提示词、主题、备注及图像元数据，并确认有对外分享权限。不要直接将整个私人归档或内部 JSON / CSV 导出复制进 GitHub。
