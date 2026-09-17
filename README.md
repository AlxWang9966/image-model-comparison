# Image Model Comparison

**Image Lab：用同一个提示词，并排比较图像生成模型的速度与画质。**

这是一个在电脑本地运行的对比工作台：输入提示词、选择模型，查看原图和真实耗时，再通过盲评与人工评分记录画质判断。每轮图片会按**主题 → 测试批次 → 模型 → 轮次**自动归档，并附上完整备注。

当前版本：**v0.4.0 · 图片编辑、MAI 2.6 与测评报告**。固定版本和下载见 [Releases](https://github.com/AlxWang9966/image-model-comparison/releases)，之前的版本保持不变。

> **给首次使用的客户**
>
> GitHub 提供源代码、使用说明和经过审核的公开示例，**不是在线生图网站，也不提供可共享的密钥或云资源**。使用者需要自行配置有权限调用的模型服务。
>
> 界面和文件保存在本地；实际生图会把提示词发送给你配置的 Azure / OpenAI 服务。**“本地运行”不等于“离线推理”或“数据不离开电脑”。**

![Image Lab 界面示例](docs/images/workbench.png)

主界面支持系列切换、最近结果与置顶回看。高级参数默认收起，非关键解释集中在 **ⓘ** 提示里，鼠标悬浮、键盘聚焦或手机点击即可查看；按 Escape 关闭。计费提示、图像发送授权和错误信息不会隐藏。

首次下载后所有模型及翻译服务默认禁用，自己的资源配置完成后才能生成。原图及逐图备注见 [公开示例](examples/README.md)。客户可先查看 [离线测评报告示例](examples/reports/README.md)，不需要配置模型。

图片编辑使用同一套工作台，只增加任务切换和一张参考原图。质量量化的方法依据、适用范围和小样本限制见 [质量评测调研与建议](docs/quality-evaluation.md)。

## 功能

| 功能 | 说明 |
| --- | --- |
| 一键系列对比 | 全部 / GPT 系列 / FLUX 系列 / MAI；同步筛选画布、统计、历史和导出 |
| 单图编辑对比 | 上传原图 → 输入修改要求 → 跑通参与模型 → 人工分项评价；不支持蒙版、多图或画笔编辑器 |
| 同任务对比 | 使用原文或明确记录的英文版本，统一共同支持的目标尺寸；可强制全模型原文 |
| 英文准备 | 内置审核过的英文预设；自定义中文可通过使用者配置的 Azure 文本模型准备英文 |
| 多轮测速 | 每个模型重复 1–10 轮；支持并行对比或串行基准 |
| 原图查看 | 并排、不裁剪显示；支持放大、原始大小和下载 |
| 盲评与评分 | 文生图保留原有人工评分；编辑按遵循、自然度、未改内容保持、文字准确性分项记录，无编辑总分 |
| 历史回看 | 最近实验卡片、重要结果置顶、按任务／模型系列筛选、恢复上次浏览位置 |
| 图片归档 | 有意义的文件名；逐图 JSON 备注；每批次 README 和 manifest |
| 测评报告 | 可离线转发的 HTML：内嵌图片、性能图表、人工分项分数、逐样本记录与基于数据的解析 |
| 工程导出 | JSON / UTF-8 CSV，方便内部分析与复现 |
| 客户端轻量 | 单文件 HTML、无需 npm / Node.js；文生图使用标准库，图片编辑额外使用 Pillow 校验和清理原图 |

## 模型范围

| 界面槽位 | 接口 | 说明 |
| --- | --- | --- |
| MAI Image 2.5 | Azure MAI images API | 已实现文本生图适配；需使用者自己的部署 |
| MAI Image 2.6 | Azure MAI images API | 标准版 Preview；独立支持文生图和单图编辑，与 2.5 并列，不覆盖旧结果 |
| GPT Image 2 | Azure / OpenAI GPT Images API | 支持当前适配器的同步 images/generations 协议 |
| FLUX.1 Kontext Pro | Azure Foundry BFL 原生 API | 也支持资源实际提供的 FLUX OpenAI-compatible Image API |
| FLUX.2 Pro | Azure Foundry `flux-2-pro` 原生 API | 多语言提示词、显式宽高、最高 2048 × 2048 |
| FLUX.2 Flex | Azure Foundry `flux-2-flex` 原生 API | 支持 steps / guidance 调整，参数与耗时一起记录 |
| GPT Image 2.5 Flare | Azure / OpenAI GPT Images API | 独立模型槽位、独立原图与耗时记录 |
| GPT Image 2.5 Sunburst | Azure / OpenAI GPT Images API | 不与 Flare 合并；使用自己的实际部署 |

不要将“代码中有一个槽位”理解为已经拥有该模型的访问权限。每个模型都必须填入自己的实际 endpoint 和 deployment。Flare / Sunburst 对应 `gpt-image-2.5-flare` / `gpt-image-2.5-sunburst`，程序不会把它们换成 GPT Image 2。FLUX 适配器针对上表三个变体，不是任意 FLUX 版本的通用适配器。

原有四槽位、六槽位和七槽位配置仍可读取，缺少的新槽位会补充为禁用状态。MAI Image 2.6 使用独立 ID `mai-image-2-6`；原有 2.5 配置和历史不改名。为兼容原有配置，Flare 模板沿用内部 ID `gpt-image-2-5`，Sunburst 使用 `gpt-image-2-5-sunburst`；界面与备注中明确显示实际模型名称。已有自定义 GPT 2.5 配置和历史名称不会自动被改写为 Flare。旧配置也不会自动开启付费翻译。

### 清楚地选择比较范围

| 顶部按钮 | 新实验默认选择 |
| --- | --- |
| GPT 系列 | GPT Image 2、GPT Image 2.5 Flare、GPT Image 2.5 Sunburst 中已配置的项 |
| FLUX 系列 | FLUX.1 Kontext、FLUX.2 Pro、FLUX.2 Flex 中已配置的项 |
| MAI | MAI Image 2.5、MAI Image 2.6 中已配置的项，可在文生图与编辑模式分别对照 |
| 全部 | 所有已配置模型，可继续取消勾选不需要的项 |

点击系列按钮会移除其他组的勾选，**不会因为隐藏了某些模型而继续把它们计入下一轮调用**。参数和勾选只影响下一轮，当前或历史实验不会被修改。

画布、最快标记、统计、明细及历史列表跟随同一个筛选。每次只查看一个实验，不从多个提示词或多个批次拼图。若当前记录没有该组，会明确显示为空，可打开本组历史或开始新实验。

已经发出的其他模型请求不会被筛选动作取消，运行进度仍注明整轮进度。JSON / CSV 导出只包含当前组的模型与样本；JSON 标记 `view_filter`，实验级信息及归档引用仍指向原始完整批次。图片归档不会因切换筛选而删除其他模型。

**高级参数**默认折叠，摘要直接显示尺寸、轮数和模式。普通使用只需选系列、填提示词、勾模型、点开始。需要固定入口时，可使用 `http://127.0.0.1:8765/?family=gpt` 或 `?family=flux`。

### 最近结果、置顶与恢复

所有实验都会保存，并不是只有置顶的实验才保留。主界面的 **已保存的实验** 显示当前任务／系列下最近的四条，置顶优先；“全部记录”可回看其余历史，失败或中断也不会被删掉。系列筛选中的缩略图来自对应系列，编辑原图可作为没有成功输出时的预览。

点击实验的 **☆ 置顶**，下次打开仍会显示；取消置顶不删除原图、评分或报告。重新打开页面会恢复上次浏览的实验、任务类型、系列与轮次；正在运行的实验优先恢复。URL 里的显式任务／系列／实验选择优先于上次浏览设置。

图片、人工评分、报告和置顶状态保存在**本地服务端文件**；浏览器 `localStorage` 只保存实验 ID 和视图位置，不保存密钥、完整图片或提示词。清除浏览器数据不会删掉实验文件；浏览器禁用存储时会提示不能恢复位置，但仍可打开完整历史。

这是本机历史，不是 GitHub 托管的客户结果库。下次访问工作台仍需启动 Python 服务并保留 `image-lab-output`；只有已经导出的 HTML 报告可以在没有服务时离线阅读。

## 1. 下载并启动

### 环境

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 或更高版本 |
| Pillow | 仅图片编辑需要：`py -3 -m pip install -r requirements-edit.txt` |
| 浏览器 | 较新的 Microsoft Edge、Chrome 等浏览器 |
| Azure CLI | 仅使用 Entra ID / `az login` 鉴权时需要 |
| 网络与权限 | 能访问选定模型服务，并具有相应推理权限与额度 |

可以在 GitHub 使用 **Code → Download ZIP**，解压后在项目文件夹打开 PowerShell；也可以克隆：

```powershell
git clone https://github.com/AlxWang9966/image-model-comparison.git
Set-Location .\image-model-comparison
.\start-image-lab.ps1
```

默认打开 **http://127.0.0.1:8765**。保留终端窗口，按 `Ctrl+C` 停止。

首次启动会从 `image-lab.config.example.json` 创建本地 `image-lab.config.json`，**不会覆盖已有配置**。模板中的订阅 ID 是占位值，所有模型均禁用，因此下载后不会自动调用任何云服务。

若不想自动打开浏览器，或默认端口已占用：

```powershell
.\start-image-lab.ps1 -Port 8766 -NoBrowser
```

启动脚本依次尝试 `py -3`、`python` 和 Windows Azure CLI 附带的 Python。也可直接启动后端：

```powershell
py -3 -m image_lab --port 8765
```

在 macOS / Linux 上可使用 `python3 -m image_lab`；Windows 启动脚本和旧 PowerShell 结果迁移主要面向 Windows。**不要双击 HTML 作为完整应用使用，也不要把它单独部署到 GitHub Pages**：它需要本地 Python API。

## 2. 配置自己的模型

推荐通过 **模型设置** 配置，而不是手工把密钥写入文件：

1. 使用 Entra ID 时，先在终端运行 `az login`。
2. 打开界面右上角的 **模型设置**，填写自己的 Subscription ID 和 Resource Group。
3. 为要使用的槽位填写完整的生成 endpoint、实际部署名 / model ID。
4. 选择鉴权方式，填写可选的模型版本标记，勾选 **启用此模型**。
5. 保存后，从左侧选中模型。建议先执行 1 轮排查接入，再做多轮对比。

**已配置不等于远端已验证。** 保存配置不调用模型；真正可用性以实际请求为准。版本标记用于记录配置，不是后端实时发现的模型版本。更换 endpoint / 部署时，UI 会清空未重新填写的旧版本标记。

### Endpoint 格式

以下都是占位示例，必须替换资源名和部署名，不能直接当作现成服务使用：

| 模型 / 接口 | 完整 endpoint 示例 |
| --- | --- |
| MAI | `https://YOUR-RESOURCE.services.ai.azure.com/mai/v1/images/generations` |
| Azure GPT Images | `https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT/images/generations?api-version=2025-04-01-preview` |
| GPT Image 2.5 Flare | `https://YOUR-RESOURCE.openai.azure.com/openai/deployments/gpt-image-2.5-flare/images/generations?api-version=2025-04-01-preview` |
| GPT Image 2.5 Sunburst | `https://YOUR-RESOURCE.openai.azure.com/openai/deployments/gpt-image-2.5-sunburst/images/generations?api-version=2025-04-01-preview` |
| Azure GPT Images v1 | `https://YOUR-RESOURCE.openai.azure.com/openai/v1/images/generations` |
| Azure FLUX Kontext 原生 BFL | `https://YOUR-RESOURCE.services.ai.azure.com/providers/blackforestlabs/v1/flux-kontext-pro?api-version=preview` |
| Azure FLUX.2 Pro | `https://YOUR-RESOURCE.services.ai.azure.com/providers/blackforestlabs/v1/flux-2-pro?api-version=preview` |
| Azure FLUX.2 Flex | `https://YOUR-RESOURCE.services.ai.azure.com/providers/blackforestlabs/v1/flux-2-flex?api-version=preview` |
| Azure FLUX Images API（资源支持时） | `https://YOUR-RESOURCE.services.ai.azure.com/openai/v1/images/generations?api-version=preview` |
| OpenAI 官方 Images API | `https://api.openai.com/v1/images/generations` |

FLUX 默认建议使用已经适配的 BFL 原生路由。部分资源的 OpenAI-compatible FLUX 路由会返回 404；不要假设所有资源都提供同一组路由。程序**不在计时请求中自动切 endpoint、换模型或重试**。

MAI 2.5 与 2.6 可使用同一个资源 endpoint，但请求中的 `model` 是各自的实际部署名，例如 `MAI-Image-2.5` / `MAI-Image-2.6`。MAI 2.6 的本次部署核对版本为 `2026-07-31`（Preview），公开模板仍不提供任何私人 endpoint 或预启用连接。这里只加入标准版，不自动添加 2.6 Flash。

为保持对比条件一致并避免额外联网，MAI 2.6 文生图和编辑请求均显式设置 **`auto_aspect_ratio=false`、`web_grounding=false`**。这两项实际参数会写入请求与归档备注；不会为追求效果暗中调用网页搜索或改变目标画幅。MAI 2.5 不发送这些 2.6 专属字段。

FLUX.2 必须使用对应的原生 BFL 路由。Flex 默认 `steps=50`、`guidance=4.5`；可在该模型的设置里改为 1–50 步、1.5–10 的 guidance。降低步数会改变质量 / 速度权衡，不能只报更快而不记录参数。Pro 不套用 Flex 的这些控制。

### 推荐：Entra ID

选择 `azure_cli`，使用当前 Azure CLI 登录获取 `https://cognitiveservices.azure.com/` 范围的访问令牌。

令牌只保留在后端内存缓存，不返回浏览器、不写入结果文件。使用者必须具有相应的**推理数据面权限**；能在 Portal 看见资源或管理部署，不一定能调用模型。遇到 403 请联系资源管理员确认权限和网络规则。

程序不会替你创建部署、分配角色、获取账户 Key、开启本地 Key 鉴权、修改安全标签或更改防火墙。

### 可选：API Key 环境变量

选择 `api_key_env`，在 UI 中只填写变量**名称**，例如 `IMAGE_LAB_API_KEY`，不要填密钥值：

| 服务 | `api_key_header` |
| --- | --- |
| Azure MAI / Azure Images API | `api-key` |
| Azure 原生 BFL API | `Authorization` |
| OpenAI 官方 API | `Authorization` |

可在启动服务的 PowerShell 中使用隐藏输入，避免把密钥明文写在命令历史里：

```powershell
$secret = Read-Host "API Key" -AsSecureString
$env:IMAGE_LAB_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
.\start-image-lab.ps1
```

服务退出后可清理本终端变量：

```powershell
Remove-Item Env:\IMAGE_LAB_API_KEY
$secret.Dispose()
Remove-Variable secret
```

API Key 模式不需要 Azure CLI。变量值仍会存在于运行进程的环境 / 内存中，应只在受信任的电脑和终端使用。修改环境变量后要重启服务。若 Azure 资源禁止 Key 鉴权，应使用 Entra ID，不要为了运行示例降低组织安全策略。

### 中文、英文与文字渲染

[BFL 官方多语言提示词指南](https://docs.bfl.ml/guides/usecases_t2i_multi_language.md)明确说明 **FLUX.2 理解多种语言**，并建议某些文化相关内容使用本地语言。该页面不是一份穷尽的中文能力保证清单；不能把“某张中文海报失败”直接解释为“整个 FLUX 家族不支持中文”。

**理解中文指令**与**在图像里准确渲染中文文字**是不同能力。中文物体 / 颜色指令、中文海报文字、复杂排版应分别评价。

新模板中 FLUX.2 Pro / Flex 默认保留原文。Kontext 的中文可靠性未在所查官方页面中建立，因此新模板提供英文优先路径；这是一项可改的实用策略，**不是 API 拒绝中文的声明**。已有旧配置仍保持原文，除非使用者明确改变策略。

本项目的[中英文配对示例](examples/flux-language-check/README.md)使用同一个“三个彩色物体”任务，不要求图内文字：Pro / Flex 的中文输出遵循了物体、颜色和布局；Kontext 的中文输出偏离任务，而英文输出遵循了任务。这里只记录一次配对观察，不能扩展成所有中文任务或中文字形的能力保证。

实验中的语言策略有三种：

| 策略 | 行为 |
| --- | --- |
| 按各模型设置 | 使用各槽位的 `prompt_policy`：`original` 或 `english` |
| 全部使用原文 | 绕过英文准备，用于中文能力 / 严格同字符串对照 |
| FLUX 系列使用英文版 | 为所有选中的 FLUX 变体使用英文对应版本，MAI / GPT 保留原文 |

对英文优先模型：有英文稿就使用英文稿；中文指令没有英文稿时，才需要自动准备。已经是英文指令、仅引号中有中文文字目标的提示词不必重复翻译。这只是发送策略的字符检查，不是完整的语言识别或能力认证。

内置预设有配对英文稿，**不增加翻译调用**。修改原文时 UI 会清除旧英文稿，避免不同任务误配。自定义英文稿由使用者检查语义一致性。

### 自动英文准备（可选、额外云端调用）

在 **模型设置 → 自动英文准备** 中启用，并填写自己的 Azure OpenAI 文本模型部署，例如支持 JSON mode 的 `gpt-4.1-mini`：

```text
https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-TEXT-DEPLOYMENT/chat/completions?api-version=2024-10-21
```

也支持 Azure 的 `/openai/v1/chat/completions` 路由。文本部署必须支持当前适配器使用的 JSON mode、`temperature` 和 `max_tokens` 参数。新模板默认禁用翻译。

每个实验最多准备一次英文稿，所有需要英文的模型与轮次复用；**原始提示词会额外发送给这个文本模型，并可能产生费用**。界面会提示这项额外调用。英文稿、准备耗时、文本 API 耗时及服务返回的 token 用量会记录到结果。

自动准备不添加或删除任务要求，引号里的文字（例如 `“春日读书会”`）必须保持原样，不能把中文排版任务偷换成英文排版。输出被截断、不是英文指令或改动了引号文字时，会明确失败，不拿未完成内容继续生图。

翻译失败时，需要英文的模型不发出生图请求，也不暗中退回中文；可使用原文的模型仍能继续。翻译请求超时或取消也可能已经计费，不自动重试。

**不同语言版本不是严格相同字符串的比较。** 每张图都记录 `original_prompt`、`effective_prompt` 和 `prompt_variant`，结果会标注语言差异，不能用原文标签掩盖实际发送的英文。归档中的英文版本图片另有 `_en` 文件名标记。

## 3. 做一次可比较的实验

1. 先点击 GPT / FLUX / MAI / 全部，再选择示例提示词或输入完整提示词。
2. 填写 **提示词主题 / Topic**，如“中文海报”“产品玻璃材质”“多物体数量关系”。它会用于归档目录、文件名和备注；留空时取提示词前 32 个字符。
3. 检查本组的模型勾选；需要调整尺寸、质量、轮数或语言时展开高级参数，必要时补充英文稿。
4. 点击 **开始对比**。生图调用数量为“模型数 × 轮数”；需要自动英文准备时再增加最多一次文本请求，均按相应服务计费。
5. 查看各轮结果，点击原图放大；使用盲评后再给画质评分。
6. 在结果下方查看归档路径。评分保存后，归档备注会同步更新。

**并行模式**每个模型最多一个请求并发执行，整轮结束后才进入下一轮。**串行模式**一次一个请求，每轮轮换模型执行顺序，减少固定先后顺序带来的偏差。

选择 MAI 2.5 / 2.6 或 FLUX.1 Kontext 时，共同目标尺寸是 **1024 × 1024**。GPT 与 FLUX.2 还支持配置内的横图 / 竖图；只选 FLUX.2 时可使用 **2048 × 2048**。Kontext 使用 `1:1` 比例，FLUX.2 发送明确的 `width` / `height`。实际返回像素尺寸都会核对、记录，不会通过暗中缩放伪造统一分辨率。

GPT 的 `low / medium / high / auto` 只适用于 GPT。MAI、FLUX 使用各自原生设置，Flex 的 steps / guidance 也不是 GPT quality，**它们不是同一个“质量档位”**。

为保持界面清晰，GPT 组目前使用共同的 1024 / 1536 尺寸档位和上述质量选项。Flare / Sunburst API 还有更高质量或自定义分辨率能力，但本版没有把这些额外控制堆到主界面。

### 如何理解速度

| 指标 | 含义 |
| --- | --- |
| `elapsed_ms` | 从发出生成请求，到原图下载 / 解码 / 检查并保存的端到端时间 |
| `api_ms` | API 请求阶段：包含请求编码／构造，到完整 API 响应读完 |
| `download_ms` | API 返回图片 URL 时，额外下载原图的时间；base64 响应通常为 0 |
| `save_ms` | 原始图片写入磁盘的时间 |
| `translation.elapsed_ms` | 英文准备总时间（包含其鉴权、文本服务调用和结果检查），不叠加到单张图耗时 |
| `translation.api_ms` | 额外文本模型 API 的请求时间 |
| 均值 / P50 / P95 | 仅使用当前实验的成功样本，P95 使用 nearest-rank 算法 |

端到端时间包含网络和图片传输，**不是纯模型推理时间**；不包含英文准备、Azure 登录、调度排队、实验元数据写入，以及后续的可读归档副本写入。base64 解码 / 检查包含在端到端时间中，因此拆分指标不必刚好相加等于总时间。

失败、限流、超时单独记录，不作为成功样本混入均值。小样本的 P95 只是描述性数字；建议多提示词、多轮复测，不要把单次最快当成稳定结论。

### 如何理解质量

文生图评分为人工 1–5 分：提示词遵循、视觉细节、构图美感、文字准确性。未评分 / 不适用的项目保持空值，不按 0 分处理。

每张图的分数是已评分项目的等权平均；模型均分是已评分图片分数的平均。建议同一对比使用相同的评分项和准则。程序不伪造初始分数，也没有未经说明的自动 AI 打分。

盲评只隐藏当前 UI 的模型身份、顺序和耗时，不是具有身份隔离能力的多用户双盲研究系统。

## 图片编辑：一条简单流程

首次使用先为启动服务的同一个 Python 安装图像校验依赖；没有 Pillow 时，文生图仍可运行，编辑界面明确提示安装，不会跳过图片校验：

```powershell
py -3 -m pip install -r requirements-edit.txt
.\start-image-lab.ps1
```

若启动脚本使用的不是 `py -3`，请用实际 Python 路径运行 `-m pip install -r requirements-edit.txt`。不要把包装到其他 Python 环境后误认为服务已经具备该依赖。

1. 切换到 **图片编辑**，选择模型系列（验证全链路时选“全部”）。
2. 上传 **一张 PNG / JPEG**，填写清楚“修改什么、保留什么”的编辑指令。
3. 确认有权限将原图及指令发送给所选云端模型，再点击 **开始编辑对比**。
4. 在一轮内查看所有结果。点击结果原图，会在同一窗口并排显示参考图和编辑结果；切换系列不混入其他任务的图像。
5. **所有参与模型和轮次成功后**才开放编辑人工评分；有失败、取消或中断则继续保留完整记录，先排查并复用原图开启新的实验。

原图限制：单帧 PNG / JPEG、最多 **8 MiB**，每边 64–4096 像素，最多 16 MP。输入会经过完整解码校验，按 EXIF 方向旋转、转换为 RGB / RGBA PNG 并移除元数据；**不缩放、不裁剪**。规范化后的 PNG 也必须小于 8 MiB。上传文件名不进入模型请求或归档。

“参考原图”指所有模型实际收到的这份规范化 PNG；上传前文件与规范化文件的 SHA-256 在私人元数据中均有记录。RGB 转换、EXIF 方向处理不是原始文件逐字节保留，源文件仍由使用者自己保管。若颜色管理有严格要求，建议先导出已确认的 sRGB PNG。

上传动作只在本地准备图片，不调用模型。点击开始后，参考图才发送给选中的模型服务；自动英文准备只发送编辑文字，**不会把原图发送给翻译模型**。输入和输出都是客户数据，不因“去掉元数据”就适合公开。

### 各模型的真实编辑方式

| 模型 | 编辑接口和输入 |
| --- | --- |
| MAI Image 2.5 / 2.6 | `/mai/v1/images/edits`，multipart 的各自 `model`、`prompt`、单张 `image`；2.6 额外明确关闭自动画幅与联网搜索 |
| GPT Image 2、Flare、Sunburst | 对应 endpoint 的 `/images/edits`，multipart 单张 `image` 与提示词／共同参数 |
| FLUX.1 Kontext、FLUX.2 Pro / Flex 原生接口 | 原来的 BFL 模型路由，新增真实参考图的 `input_image` base64 数据 |
| 资源支持的 Kontext Images API | `/images/edits` 的 multipart 单图请求 |

编辑接口从已配置的生成 endpoint 推导，不需要额外堆一套配置表。使用自己资源实际提供的接口；服务返回不支持／404 时明确失败，**不换模型、不退回纯文生图**。

建议先用 **1024 × 1024** 的源图和同样目标尺寸打通所有模型。参考图本地不调整尺寸；MAI 的编辑协议使用原生输出几何，其他模型可能依据目标尺寸生成。输入／请求／输出尺寸均有记录；输出与目标不同会提示，不能当严格等像素比较。

### 计时与评分口径

编辑端到端时间包含 multipart / base64 请求编码、参考图上传至模型、模型处理、结果下载／解码及原图保存；不含浏览器上传到本地服务、输入规范化、鉴权、英文准备和归档副本写入。

**接口成功只能证明调用完成，不能证明编辑正确。** 编辑人工表有四个独立维度：指令遵循、结果自然度、未改内容保持、文字准确性。每项 1–5 分，未评／不适用为空；编辑结果只展示各项均值及样本数，**没有整体质量分数**。全图改变风格等任务的“未改内容保持”可记 N/A。

本轮 gate 使用全部参与模型和全部轮次，而非当前筛选可见的子集；只切到已成功的 GPT 组不能绕过其他模型失败。现有文生图评分行为不变。当前仍没有 FID、LPIPS、OCR 或视觉 LLM 自动评分；未来是否接入及怎么验证，见 [调研文档](docs/quality-evaluation.md)。

### 参考图与编辑归档

参考图的本地准备文件位于 `image-lab-output\_references\<id>\`；每次编辑实验另外保存独立的 `reference.png`。后续换图不会改变已有实验。读取复用图片及发出请求前核对 SHA-256，输入被修改时不会带着错误图片继续请求。

编辑可读归档使用 `edit-<topic>-<hash>` 主题目录，批次中有 `reference.png`、`manifest.json`、每个模型的结果与备注。备注包含 `operation=edit`、相同源图哈希、原始／实际指令、实际参数、结果和分项评分。它不会被文生图统计混用。

完整本地归档仍默认排除在 Git 之外。输入、结果、指令与评分都可能包含客户资料；本次通路示例只是非敏感联调，不代替导师正在收集的独立客户样本。

### 停止、刷新与异常

停止只阻止尚未发送的请求，已发送请求会继续完成并可能计费。刷新页面会重新连接活动实验。重启后端会将未完成任务标记为中断，**不会自动重复可能已经计费的调用**。

## 测评报告：导出、解析与可视化

**自动量化评分仍暂缓。** 报告使用真实请求数据和已有人工评分，不增加视觉裁判、自动质量模型或图像生成调用；“自动汇总报告”不等于“自动给图片评分”。

### 如何导出

1. 打开一个已经结束的实验，选择全部模型或需要的模型系列；盲评时先揭晓身份。
2. 点击 **导出报告**，选择是否包含结果图片／编辑原图、是否包含人工文字备注。
3. 确认报告可能带有客户数据，分享前需要审核。
4. 点击 **生成并下载 HTML**。得到一个自包含文件，可离线打开，也可在报告里 **打印 / 另存为 PDF**。

无人工评分也能导出：分数栏明确为 **未评分 / N/A**，解析会说明证据不足，绝不会用 0 分或虚构分数填充。编辑任务尚未全员成功时，只可导出已有运行证据，报告保留评分锁定状态；失败、取消和中断不会被当作成功。

### 报告包含什么

| 部分 | 内容 |
| --- | --- |
| 实验设置 | 类型、时间、状态、当前系列范围、目标尺寸、轮数、执行方式、计时口径 |
| 真实输入 | 原始提示词、英文对应版本、逐模型实际指令；编辑时可内嵌共同原图和哈希 |
| 性能可视化 | 成功样本平均耗时条形图、成功 / 计划样本数、均值 / P50 / P95 / 最短 / 最长 |
| 人工分数比较 | 各维度的均值、固定 5 分刻度的色条及有效样本数；空值保持空白含义 |
| 逐样本明细 | 各轮原图、模型版本、真实参数、耗时拆分、状态、人工分项分数与可选评语 |
| 基于记录的解析 | 本批次速度差异、评分覆盖不足、样本量、失败、语言／尺寸差异等明确事实 |
| 解释与限制 | 非稳定排行榜、非认证分、独立样本限制、旧计时口径、分享隐私边界 |

解析是确定性文字汇总，**不自动“看图”判断材质、画面美观或编辑正确性**。它只引用已存的计时和人工分数。各模型评审覆盖不足、已评数量或轮次不一致时不生成质量排名结论；有一致覆盖时也只说“这些记录中的均值”，不宣称统计显著或普遍更好。

编辑报告没有综合质量分，文生图报告也会展开各个人评维度，以便看清来源。不同 prompt、不同实验或旧／新计时口径不会自动拼成一个排名。

### 保存与限制

生成的 HTML 同时留存在：

```text
image-lab-output\<experiment-id>\reports\<report-id>.html
```

这是导出时的**不可变快照**。之后修改评分、备注或置顶不会覆盖以前的报告；需要更新时重新导出，旧报告仍可在导出窗口中再次下载。记录包含来源实验更新时间、导出时间、范围、选项、大小和 SHA-256，下载时检查文件完整性。

内嵌原图总预算为 **32 MiB**；base64 会增加 HTML 体积。超过预算时明确提示，选择更小的系列范围或取消“包含图片”再导出，不会悄悄漏图。选择包含图片但某张原图缺失、大小／哈希不匹配时，在报告中显式说明，不拿其他图替代。打印 PDF 由浏览器完成，不另开云端转换服务，分页以浏览器为准。

报告去掉 endpoint、部署别名、凭据、绝对路径和原始错误正文，只保留必要的错误状态 / HTTP 代码；**图像、prompt、主题、模型显示名称与评语仍可能敏感**。生成图按保存的原始字节嵌入，可能含元数据，不在报告中偷偷改图。分享前仍需人工审核，取消“包含人工备注”也不等于已经全面脱敏。

## 4. 图片文件夹与完整备注

本地保留两套互补的结果：

| 目录 | 用途 | 默认上传 GitHub？ |
| --- | --- | --- |
| `image-lab-output` | 应用的原始结果和配置快照；图片使用不暴露模型身份的文件名 | 否 |
| `image-lab-output\<id>\reports` | 保存的离线 HTML 报告快照 | 否 |
| `image-lab-archive` | 按主题、批次、模型、轮次整理的可读图片副本和备注 | 否 |
| `examples` | 维护者人工挑选、审核后明确发布的示例 | 是 |

可读归档示意：

```text
image-lab-archive\
  README.md
  chinese-bookstore-poster-<topic-hash>\
    20260910-080000-<experiment-suffix>\
      README.md
      manifest.json
      mai-image-2-5\
        round-01_mai-image-2-5_chinese-bookstore.png
        round-01_mai-image-2-5_chinese-bookstore.json
      gpt-image-2\
        round-01_gpt-image-2_chinese-bookstore.png
        round-01_gpt-image-2_chinese-bookstore.json
      flux-1-kontext-pro\
        round-01_flux-1-kontext-pro_chinese-bookstore.png
        round-01_flux-1-kontext-pro_chinese-bookstore.json
```

每批次用创建时间和独立 ID 区分，不覆盖上一轮。目录时间使用记录中的时区（新实验保存为 UTC），完整时间戳写在备注里。文件名会清理 Windows 不支持的字符并限制长度，完整主题与提示词不会因此从备注中丢失。

每张图片旁的 `.json` 是可移植的备注文件，而不是依赖 Windows 专有文件属性：

| 字段 | 内容 |
| --- | --- |
| `topic` / `original_prompt` | 主题与完整原始任务提示词；旧记录未知时明确为空 |
| `prompt` / `effective_prompt` / `prompt_variant` | 该样本实际准备的提示词与语言版本；结合状态及 `image_request_started` 判断是否已开始调用 |
| `model` | 模型名称、固定模型 ID、提供方、记录的版本 |
| `experiment_id` / `round` | 测试批次与轮次 |
| `request_parameters` | 尺寸、质量、数量、格式、比例以及 Flex steps / guidance 等实际参数 |
| `timing` | 计时口径与各项毫秒耗时 |
| `image` | 文件位置、真实像素尺寸、字节数、SHA-256 |
| `operation` / `reference_image` | 文生图或编辑；编辑时包含实际共同输入的文件、尺寸与 SHA-256 |
| `human_review` | 文生图使用原有四项；编辑为遵循、自然度、未改内容保持、文字；未填时为空 |
| `status` / `diagnostic_note` | 成功、失败或旧图丢失情况 |

`manifest.json` 汇总整批样本，批次 `README.md` 提供可直接阅读的说明与原图 / 备注链接。图片是原始结果的逐字节副本，不为美观改图。

每轮结束后自动整理；保存评分后同步备注；界面上的 **更新归档** 可重新整理已有结果，不会重新生图。如果有人修改了归档图片，程序会报错而不是覆盖修改后的副本。请把手工修图另存，程序生成的备注应通过 UI 更新。

### 旧记录

启动时可导入项目目录及直接子目录中的 `benchmark-results-*.json`，并匹配 `request-manifest-*.json`。公开代码仓库不包含维护者的私人历史记录或旧 Azure 管理脚本。

旧脚本曾复用图片名，因此有些图片已被后续测试覆盖。只在报告、文件大小和时间能够对应时复制旧图；否则保留记录并标为 `not_retained`，**不会拿其他图片顶替**。没有 manifest 的旧提示词、质量和尺寸保持未知。

旧记录的 `legacy_api_response` 在 API 响应后停止计时，不含解码 / 保存；不能与新实验的 `end_to_end` 混合排名。

## 5. 隐私与对外分享

**公开代码与公开业务数据是两回事。**

本仓库使用 Git 白名单，只发布应用、配置模板、测试、说明和经过审核的示例；个人配置、原始结果、完整本地归档、日志、密钥文件、旧管理脚本默认不受 Git 跟踪。不要使用 `git add -f` 强行上传这些内容。

| 对象 | 处理方式 |
| --- | --- |
| Azure 订阅 / 资源信息 | 使用者自己的本地配置，不放入公开模板或示例 |
| API Key / Entra Token | 后端内存或进程环境变量；不放浏览器、源码或备注 |
| 云端连接字段 | 可读图片归档不包含 endpoint、部署别名、订阅 ID、完整错误或绝对源路径 |
| 提示词、图像、主题、人工备注 | 仍可能是客户数据；默认只留本地，外发前必须人工审核 |
| 自动英文准备 | 仅在明确启用后使用；原文会发送至使用者配置的额外 Azure 文本部署，英文稿也按客户数据保护 |
| UI JSON / CSV 工程导出 | 可能含部署名和 endpoint 等配置快照，适合内部复现，不等于脱敏分享包 |
| HTML 测评报告 | 去掉连接配置与原始错误正文；可含图像、prompt 和评语，导出前确认并在外发前审核 |
| `examples` | 只放明确审核过的固定示例，不自动收集后续生成结果 |

给客户分享结果时，优先从可读归档中挑选批次，审查图片里的文字 / 人脸 / 标识、完整提示词、主题、备注及可能的图像元数据，再复制到单独的分享目录。公开示例已排除个人连接信息及人工评语，并检查图片元数据；这不代表未来任意输出可以自动公开。

本服务仅绑定 `127.0.0.1`，采用同源检查和每进程请求令牌，仅提供 UI 与归属已知实验的图片。图片 URL 下载不携带模型凭据，且限制下载域名和重定向。**不要直接暴露到公网，也不要将本地版本当作已有用户隔离、访问审计或企业合规认证的 SaaS 服务。**

## 6. 常见问题

| 问题 | 处理 |
| --- | --- |
| 首次看到 `0 / 8` 已配置 | 正常：模板默认禁用所有模型，先配置自己的资源并启用 |
| 想找上次的结果 | 查看“已保存的实验”或“全部记录”；确认当前任务／模型系列、“只看置顶”筛选以及本地结果目录 |
| 导出按钮不可用 | 等待本轮结束，退出盲评，并选择有结果的模型系列 |
| 报告没有质量分数 | 该项尚未人工评分或不适用；自动量化评分仍暂缓，报告不会补分 |
| 报告图片超过预算 | 缩小模型系列范围，或取消“包含图片”后导出 |
| 报告分数没有随网页变化 | 报告是不可变快照，请按最新记录重新导出 |
| MAI 2.6 显示待配置 | 旧配置升级只添加禁用入口，不创建云端部署；先部署，再填写实际 endpoint / 部署名并启用 |
| 切换 GPT 后没有图片 | 当前记录没有 GPT 结果；可打开本组历史或开始本组新实验，不会挪用其他实验图片 |
| 503 / 本地服务暂时繁忙 | 本地 HTTP 工作线程有上限，稍后重试；预览连接会及时关闭，避免长期开页积累闲置线程 |
| 中文指令需要英文稿 | 使用内置配对英文稿、手工提供英文，或配置并启用自动英文准备；也可明确选择全部原文测试 |
| 编辑提示缺少 Pillow | 使用服务实际运行的 Python 安装 `requirements-edit.txt`，再重启；不会静默跳过图片校验 |
| 无法上传原图 | 检查 PNG/JPEG、单帧、8 MiB 及像素限制；重新导出正常图像，不通过改扩展名伪装格式 |
| 编辑评分按钮禁用 | 全部参与模型和轮次都成功后才开放；筛选到成功子集不会绕过整轮条件 |
| 英文准备失败 / 引号文字改变 | 需要英文的图像模型不会调用；检查文本部署权限 / 协议或手工给出经审核的英文稿 |
| 找不到 Python | 安装 Python 3.10+；也可使用 Azure CLI 自带的运行时 |
| PowerShell 不允许运行脚本 | 遵循组织策略；可使用 `py -3 -m image_lab`，无需修改机器执行策略 |
| Azure 登录失败 / 401 | 确认 `az login`、订阅和所选鉴权方式；Key 模式检查服务端变量 |
| 403 | 确认推理数据面权限、资源网络规则和组织政策；不要自动降低安全设置 |
| 404 | 核对完整路由、API 版本与部署名；FLUX 资源未必提供 Images API 路由 |
| 429 | 触发服务限流；减少并发 / 轮数，稍后重新建实验；不会暗中重试 |
| 请求超时 | 默认 300 秒；云端任务可能仍在完成 / 计费，不要无判断连续重发 |
| 返回尺寸不一致 | 以实际尺寸与警告为准，不算严格的等像素对比 |
| 旧图缺失 | 原脚本可能已覆盖同名文件；记录会保留，但无法还原已丢失内容 |
| 归档报错 | 原始结果保留；检查磁盘、目录权限或手工修改的归档图片，再点更新归档 |
| 提示另一个实例在运行 | 同配置、结果或归档目录使用 OS 文件锁；使用现有实例或先停止它 |
| 后端重启后请求令牌失效 | 刷新页面，获取新的本地请求令牌 |

超时可在本地配置中设置为 30–600 秒。默认结果目录可以在直接启动时指定：

```powershell
py -3 -m image_lab --port 8765 --output-dir .\private-results --archive-dir .\private-archives --no-import
```

请将自定义结果目录放在受控的私人位置，**不要指向 `examples`、`docs` 等公开跟踪目录**。

## 开发与项目结构

```text
image-model-comparison\
  image-lab.html                 # 自包含中文 UI
  start-image-lab.ps1             # Windows 启动入口
  image-lab.config.example.json   # 安全的空配置模板
  requirements-edit.txt           # 编辑输入校验用的 Pillow（文生图不需要）
  image_lab\
    server.py                    # 本地 HTTP API、调度与启动
    providers.py                 # 模型适配、鉴权与响应处理
    prompts.py                   # 英文准备、引号文字保护和语言发送策略
    references.py                # 单图输入校验、元数据清理、私有源图管理
    store.py                     # 原始结果、评分和旧记录导入
    archive.py                   # 可读图片归档和逐图备注
    reports.py                   # 离线报告、分项图表与基于记录的解析
    report_template.html         # 报告主题与打印版式（不依赖 CDN）
    locking.py                   # 多实例文件锁
  tests\                         # 标准库 unittest 回归用例
  examples\                      # 审核后的公开图片与说明
  docs\images\                   # 文档截图
```

运行完整回归用例需安装编辑依赖，但不需要私人配置、Azure 登录或模型调用权限，也不会产生模型费用：

```powershell
py -3 -m pip install -r requirements-edit.txt
py -3 -m unittest discover -s tests -v
```

开发者可另外运行浏览器冒烟检查（需要已安装的 Python Playwright 和 Microsoft Edge，不是应用运行依赖）：

```powershell
py -3 .\tests\browser_smoke.py --channel msedge
py -3 .\tests\browser_edit_smoke.py --channel msedge
py -3 .\tests\browser_report_smoke.py --channel msedge
```

它们在临时目录启动独立本地服务、使用模拟图像提供方，检查系列选择、编辑、评分锁定、归档、报告离线浏览 / 打印、置顶、视图恢复、信息提示和手机布局。不会调用 Azure / OpenAI，也不会改写私人配置或真实结果。

本版本提供文生图与单图指令编辑。未提供蒙版／画笔／多图编辑器、自动 AI 评分、计费金额估算、任意第三方代理接入或云端多用户托管。

### 官方接口参考

- [MAI image API](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-mai-image)
- [Foundry FLUX API](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-flux)
- [Azure GPT image generation](https://learn.microsoft.com/azure/foundry/openai/how-to/dall-e?pivots=rest-api)
