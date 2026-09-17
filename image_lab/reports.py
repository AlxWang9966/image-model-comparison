"""Offline reports derived only from recorded measurements and human reviews."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import re
from pathlib import Path
from string import Template
from typing import Any

from . import __version__
from .providers import MAX_IMAGE_BYTES, ProviderError, ValidationError, image_info
from .store import ACTIVE_JOBS, RunStore, filter_job, now_iso, rating_fields


MAX_EMBEDDED_BYTES = 32 * 1024 * 1024
TEMPLATE = Path(__file__).with_name("report_template.html")
FIELD_NAMES = {
    "adherence": "指令遵循", "visual": "视觉细节 / 自然度",
    "composition": "构图与美感", "preservation": "未改内容保持", "text": "文字准确性",
}
STATUS_NAMES = {
    "success": "成功", "completed": "已完成", "failed": "失败",
    "cancelled": "已取消", "interrupted": "已中断", "pending": "未开始",
    "preparing": "准备中", "running": "进行中", "queued": "排队中",
}
PARAMETER_KEYS = (
    "width", "height", "size", "quality", "n", "num_images", "output_format",
    "aspect_ratio", "steps", "guidance", "auto_aspect_ratio", "web_grounding",
)


def escape(value: Any) -> str:
    return html.escape(str(value) if value is not None else "", quote=True)


def number(value: Any, digits: int = 2) -> str:
    if type(value) not in (int, float) or not math.isfinite(value):
        return "未记录"
    return f"{value:.{digits}f}"


def seconds(value: Any) -> str:
    return number(value / 1000) + " s" if type(value) in (int, float) and math.isfinite(value) else "未记录"


def effective_prompt(sample: dict[str, Any]) -> str | None:
    value = sample.get("effective_prompt", sample.get("request", {}).get("prompt"))
    return value if isinstance(value, str) and value else None


def table(headers: list[str], rows: list[list[str]]) -> str:
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


class ReportImages:
    def __init__(self, store: RunStore, job: dict[str, Any], enabled: bool) -> None:
        self.store = store
        self.job = job
        self.enabled = enabled
        self.used_bytes = 0
        self.issues: list[str] = []

    def image(self, filename: str | None, label: str, expected: dict[str, Any]) -> str:
        if not self.enabled:
            return '<div class="image-placeholder">此报告未内嵌图片</div>'
        if not filename:
            return '<div class="image-placeholder">此样本没有可用的保留图片</div>'
        try:
            path = self.store.image_path(self.job["id"], filename)
        except KeyError:
            self.issues.append(f"{label}：图片文件未保留或已移走。")
            return '<div class="image-placeholder">图片文件缺失，未使用其他图像替代</div>'
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES or self.used_bytes + size > MAX_EMBEDDED_BYTES:
            raise ValidationError("内嵌图片超过报告的 32 MiB 原图预算。请取消“包含图片”或缩小模型范围后再导出。")
        content = path.read_bytes()
        if len(content) != size:
            raise ValidationError("导出时图片文件发生变化，请重试。")
        declared = expected.get("output_bytes", expected.get("bytes"))
        digest = hashlib.sha256(content).hexdigest()
        if ((declared is not None and declared != size)
                or (expected.get("sha256") and expected["sha256"] != digest)):
            self.issues.append(f"{label}：图片与保存的大小 / 哈希不匹配，没有将其嵌入报告。")
            return '<div class="image-placeholder">图片与记录不一致，已明确排除</div>'
        try:
            extension, width, height = image_info(content)
        except ProviderError:
            self.issues.append(f"{label}：图片格式无法确认，没有将其嵌入报告。")
            return '<div class="image-placeholder">图片格式异常</div>'
        if expected.get("width") and (width != expected["width"] or height != expected.get("height")):
            self.issues.append(f"{label}：图片实际尺寸与记录不一致。")
            return '<div class="image-placeholder">图片尺寸与记录不一致，已明确排除</div>'
        self.used_bytes += size
        mime = "image/png" if extension == "png" else "image/jpeg"
        encoded = base64.b64encode(content).decode("ascii")
        return (
            f'<img src="data:{mime};base64,{encoded}" alt="{escape(label)}">'
            f'<p class="caption mono">SHA-256 {digest}</p>'
        )


def report_insights(job: dict[str, Any]) -> list[str]:
    models = {model["id"]: model for model in job["models"]}
    stats = job["summary"]
    valid = [row for row in stats if row["mean_ms"] is not None]
    notes = []
    if len(valid) >= 2:
        minimum = min(row["mean_ms"] for row in valid)
        winners = "、".join(models[row["model_id"]]["name"] for row in valid if row["mean_ms"] == minimum)
        notes.append(f"本实验成功样本的平均耗时最低为 {winners}（{seconds(minimum)}）。这是当前批次的描述性结果，不代表稳定性能排名。")
    elif valid:
        notes.append("当前范围只有一个模型有成功计时，不能进行跨模型速度优劣判断。")
    else:
        notes.append("当前范围没有成功的计时样本，无法比较生成速度。")
    failures = sum(row["status"] in ("failed", "interrupted") for row in job["samples"])
    cancelled = sum(row["status"] == "cancelled" for row in job["samples"])
    notes.append(f"本报告包含 {len(job['samples'])} 条计划样本记录；其中 {failures} 条失败 / 中断，{cancelled} 条取消。失败耗时不计入成功样本均值。")
    if any(row["successes"] < 5 for row in stats):
        notes.append("至少一个模型少于 5 张成功图，P95 仅供描述，不能据此推断总体表现。即使有更多重复，同一个 prompt / 源图也不等于多个独立客户案例。")
    if job.get("operation") == "edit" and not job.get("review_ready"):
        notes.append("原始编辑实验尚未全部成功，人工评分未开放；筛选出成功模型也不改变此条件。本报告不推断编辑质量。")
    elif not any(row["rated_count"] for row in stats):
        notes.append("当前没有人工质量评分，无法给出画质优劣结论。报告不会补分，也没有自动视觉判断。")
    else:
        for field in rating_fields(job):
            values = [row for row in stats if row["rating_dimensions"][field]["count"]]
            if len(values) < len(stats) or len(values) < 2:
                notes.append(f"{FIELD_NAMES[field]}：评分覆盖不足，未作跨模型优劣结论。")
                continue
            counts = {row["rating_dimensions"][field]["count"] for row in values}
            if len(counts) != 1:
                notes.append(f"{FIELD_NAMES[field]}：各模型已评样本数不同，仅展示各自均值，不给出排名。")
                continue
            reviewed_rounds = [
                {sample["round"] for sample in job["samples"]
                 if sample["model_id"] == row["model_id"] and sample["status"] == "success"
                 and type((sample.get("rating") or {}).get(field)) is int}
                for row in values
            ]
            if any(rounds != reviewed_rounds[0] for rounds in reviewed_rounds[1:]):
                notes.append(f"{FIELD_NAMES[field]}：各模型已评轮次不一致，仅展示分项记录，不给出排名。")
                continue
            highest = max(row["rating_dimensions"][field]["mean"] for row in values)
            leaders = "、".join(models[row["model_id"]]["name"] for row in values if row["rating_dimensions"][field]["mean"] == highest)
            count = next(iter(counts))
            notes.append(f"{FIELD_NAMES[field]}：当前已评均值最高为 {leaders}（{number(highest)} / 5，每模型 {count} 张已评）；仅反映保存的人评记录，不是显著性结论。")
    variants = {effective_prompt(sample) for sample in job["samples"] if effective_prompt(sample)}
    if len(variants) > 1:
        notes.append("模型使用了不同语言或文字版本的指令。实际输入在逐样本记录中列出，这不是严格相同字符串的对比。")
    target_known = isinstance(job["size"], str) and bool(re.fullmatch(r"\d+x\d+", job["size"]))
    mismatched = [
        sample for sample in job["samples"]
        if target_known and sample.get("width") and f"{sample['width']}x{sample['height']}" != job["size"]
    ]
    if mismatched:
        notes.append(f"{len(mismatched)} 张输出的实际尺寸与目标尺寸不同，不能当作严格等像素性能比较。")
    if job["source"] == "legacy":
        notes.append("旧脚本计时截止到 API 响应，不含解码与保存，不能与新实验端到端耗时混合排名。旧记录缺失的参数或图像保持未知。")
    return notes


def render_report(
    store: RunStore, identifier: str, provider: str = "all", *,
    include_images: bool = True, include_notes: bool = True,
    snapshot: dict[str, Any] | None = None,
) -> bytes:
    original = snapshot if snapshot is not None else store.get(identifier)
    if original["id"] != identifier:
        raise ValidationError("Report snapshot does not belong to the requested experiment.")
    if original["status"] in ACTIVE_JOBS:
        raise ValidationError("实验仍在运行，请等待本轮结束后再导出完整报告。")
    job = filter_job(original, provider)
    if type(include_images) is not bool or type(include_notes) is not bool:
        raise ValidationError("Report image/notes options must be booleans.")
    images = ReportImages(store, job, include_images)
    models = {model["id"]: model for model in job["models"]}
    fields = rating_fields(job)
    editing = job.get("operation") == "edit"
    title = job.get("topic") or "图像模型对比"
    scope = {"all": "全部模型", "gpt": "GPT 系列", "flux": "FLUX 系列", "mai": "MAI 系列"}[provider]
    success_count = sum(sample["status"] == "success" for sample in job["samples"])
    rated_count = sum(row["rated_count"] for row in job["summary"])
    parts = [
        '<div class="notice">数据来源：实际请求计时 + 已保存的人工评分。自动量化评测已暂缓；本报告没有 AI 裁判分，也不是官方认证。</div>',
        '<div class="stats">'
        f'<div class="stat"><strong>{len(job["models"])}</strong><span>模型 · {escape(scope)}</span></div>'
        f'<div class="stat"><strong>{success_count} / {len(job["samples"])}</strong><span>成功样本 / 计划样本</span></div>'
        f'<div class="stat"><strong>{rated_count}</strong><span>已有人工评分的图片</span></div>'
        f'<div class="stat"><strong>{job["runs"]}</strong><span>重复轮数 · 单个任务</span></div></div>',
        '<section class="panel"><h2>01 · 实验设置</h2>',
        table(["项目", "记录"], [
            ["创建时间", escape(job["created_at"])],
            ["实验记录更新时间", escape(job["updated_at"])],
            ["记录状态", escape(STATUS_NAMES.get(job["status"], job["status"]))],
            ["任务 / 模式", ("图片编辑" if editing else "文生图") + " / " + ("并行" if job["mode"] == "parallel" else "串行")],
            ["筛选范围", f"{escape(scope)} · {len(job['models'])} / {len(original['models'])} 个原始参与模型"],
            ["目标尺寸", escape(job["size"])],
            ["GPT quality", escape(job["gpt_quality"]) + "（仅 GPT，非跨模型等价质量档位）"],
            ["计时口径", escape(job["timing_scope"])],
            ["输入数量", "1 张共同参考图" if editing else "无参考图"],
            ["报告内容", ("内嵌原图" if include_images else "不含图片") + " / " + ("包含人工备注" if include_notes else "不含人工备注")],
        ]),
        '<h3 style="margin-top:20px">原始任务提示词</h3>',
        f'<div class="prompt">{escape(job.get("prompt") or "原记录未保存提示词")}</div>',
    ]
    if job.get("english_prompt"):
        parts += ['<h3>保存的英文对应版本</h3>', f'<div class="prompt">{escape(job["english_prompt"])}</div>']
    translation = job.get("translation")
    if isinstance(translation, dict) and translation.get("request_attempted"):
        parts.append(f'<p class="muted">英文准备：{escape(translation.get("status"))} · {seconds(translation.get("elapsed_ms"))}，未计入单张图片耗时。</p>')
    parts.append("</section>")
    reference = job.get("reference_image")
    if editing and isinstance(reference, dict):
        source_image = images.image("reference.png", "共同参考原图", reference)
        parts.append(
            '<section class="panel"><h2>02 · 共同参考原图</h2><div class="source">'
            f'<div>{source_image}</div><div><p>{escape(reference.get("width"))} × {escape(reference.get("height"))} · 所有模型收到同一份规范化 PNG</p>'
            f'<p class="muted">{escape(reference.get("normalization", ""))}</p>'
            '<p class="muted">未缩放或裁剪；输入规范化与模型输出质量是不同概念。原始实验独立保留源图。</p></div></div></section>'
        )
    insights = report_insights(job)
    parts.append('<section class="panel"><h2>结果解析 · 基于记录</h2><p class="muted">以下为确定性汇总，不是对图像内容的自动视觉推断。</p>'
                 + "".join(f'<p class="insight">{escape(note)}</p>' for note in insights) + "</section>")
    valid = [row for row in job["summary"] if row["mean_ms"] is not None]
    maximum = max((row["mean_ms"] for row in valid), default=1)
    minimum = min((row["mean_ms"] for row in valid), default=None)
    bars = []
    rows = []
    for row in job["summary"]:
        name = escape(models[row["model_id"]]["name"])
        mean = row["mean_ms"]
        fraction = max(0, min(100, mean / maximum * 100)) if mean is not None and maximum > 0 else 0
        bar = f'<div class="fill {"best" if mean == minimum and mean is not None else ""}" style="width:{fraction:.3f}%"></div>' if mean is not None else ""
        bars.append(f'<div class="bar-row"><span>{name}</span><div class="track">{bar}</div><span class="value">{seconds(mean)}</span></div>')
        total = sum(sample["model_id"] == row["model_id"] for sample in job["samples"])
        rows.append([
            name, f'{row["successes"]} / {total}', seconds(mean), seconds(row["median_ms"]),
            seconds(row["p95_ms"]), seconds(row["min_ms"]), seconds(row["max_ms"]),
        ])
    parts.append('<section class="panel"><h2>性能可视化</h2><p class="muted">条形长度 = 成功样本平均耗时；越短表示本批次更快，不代表质量更高。</p>'
                 + "".join(bars) + table(["模型", "成功 / 总数", "均值", "P50", "P95", "最短", "最长"], rows) + "</section>")
    score_rows = []
    for row in job["summary"]:
        cells = [escape(models[row["model_id"]]["name"])]
        for field in fields:
            dimension = row["rating_dimensions"][field]
            mean, count = dimension["mean"], dimension["count"]
            if mean is None:
                cells.append('<span class="empty">未评分 / N/A</span><div class="caption">0 张已评</div>')
            else:
                cells.append(
                    f'<span class="score-value">{number(mean)}</span> / 5 <span class="caption">· {count} 张</span>'
                    f'<div class="score-bar"><span style="width:{max(0, min(100, mean * 20)):.2f}%"></span></div>'
                )
        score_rows.append(cells)
    parts.append('<section class="panel"><h2>人工分数比较</h2><p class="muted">1–5 分；空值不是 0 分。每项只计算该项已评样本，色条固定按 5 分满刻度显示。编辑任务不合成总分。</p>'
                 + table(["模型", *[FIELD_NAMES[field] for field in fields]], score_rows)
                 + ("<div class=\"notice warning\">本实验尚未全部完成成功，编辑评分未开放。</div>" if editing and not job.get("review_ready") else "")
                 + "</section>")
    for round_number in sorted({sample["round"] for sample in job["samples"]}):
        parts.append(f'<section class="panel"><h2>第 {round_number} 轮 · 原图与逐样本记录</h2><div class="samples">')
        for sample in [row for row in job["samples"] if row["round"] == round_number]:
            model = models[sample["model_id"]]
            label = f"{model['name']} · 第 {sample['round']} 轮"
            image = images.image(sample.get("filename"), label, sample) if sample["status"] == "success" else '<div class="image-placeholder">本次请求没有成功输出图片</div>'
            parameters = {
                key: sample.get("request", {})[key] for key in PARAMETER_KEYS if key in sample.get("request", {})
            }
            ratings = sample.get("rating") or {}
            dims = " × ".join(str(sample[key]) for key in ("width", "height")) if sample.get("width") else "未记录"
            rows = [
                ["状态", escape(STATUS_NAMES.get(sample["status"], sample["status"]))],
                ["模型版本", escape(model.get("version") or "未记录")],
                ["端到端 / 旧口径耗时", seconds(sample.get("elapsed_ms"))],
                ["API / 下载 / 保存", " / ".join(seconds(sample.get(key)) for key in ("api_ms", "download_ms", "save_ms"))],
                ["实际尺寸", escape(dims)],
                ["指令版本", escape(sample.get("prompt_variant", "original"))],
            ]
            if sample.get("error"):
                status_match = re.search(r"\bHTTP\s+(\d{3})\b", str(sample["error"]))
                rows.append(["错误信息", f"HTTP {status_match[1]}" if status_match else "请求未成功；完整诊断保留在本地原始记录。"])
            rows.extend([FIELD_NAMES[field], number(ratings[field]) + " / 5" if ratings.get(field) is not None else "未评分 / N/A"] for field in fields)
            parts.append(
                f'<article class="sample"><h3>{escape(model["name"])}</h3>{image}{table(["记录项", "值"], rows)}'
                '<h3 style="margin-top:16px">实际输入指令</h3>'
                f'<div class="prompt">{escape(effective_prompt(sample) or "未准备完成 / 原记录未保存")}</div>'
                f'<p class="caption">实际参数（不含连接信息）：</p><div class="prompt mono">{escape(json.dumps(parameters, ensure_ascii=False, sort_keys=True))}</div>'
            )
            if include_notes:
                parts.append(f'<h3>人工观察备注</h3><div class="note">{escape(ratings.get("notes") or "未填写")}</div>')
            parts.append("</article>")
        parts.append("</div></section>")
    if images.issues:
        parts.append('<section class="panel"><h2>图片完整性说明</h2>' + "".join(f'<p class="notice warning">{escape(issue)}</p>' for issue in images.issues) + "</section>")
    parts.append(
        '<section class="panel"><h2>方法、限制与分享边界</h2>'
        '<p>这是单个实验、单个原始任务的记录，不混入其他 prompt、源图或不同计时口径。失败、取消和中断保留在分母，成功均值不使用失败耗时。P50 为中位数，P95 使用 nearest-rank。</p>'
        '<p>新实验计时包含网络、请求编码、模型处理、结果下载 / 解码和保存，不含鉴权、英文准备、本地输入规范化或归档副本。旧脚本仅计到 API 响应，不能与新实验直接混比。</p>'
        '<p>人工评分仅说明保存的评审意见，不代表多评审者共识；数量、评分覆盖和标准不一致时不应据此宣称优劣。没有自动质量指标或新增 AI 解析，也不提供 FID、SSIM、LPIPS、OCR 等未实现分数。</p>'
        '<p>内嵌图为保留文件的副本，可能自带图像元数据；本报告不会自动清理生成图，以免改变评测证据。连接 endpoint、部署别名、凭据、绝对路径和原始错误信息不包含在分享版，但提示词、图像、模型名称、主题和人工备注仍需人工审核。</p>'
        '<p>报告可离线打开；图片若未内嵌或文件丢失，将明确显示占位说明，不会从外部下载替代图。浏览器“打印 / 另存为 PDF”可生成打印副本，打印效果以浏览器为准。</p></section>'
    )
    template = Template(TEMPLATE.read_text(encoding="utf-8"))
    content = template.substitute(
        title=escape(title), subtitle=escape(("图片编辑" if editing else "文生图") + " · " + scope + " · " + STATUS_NAMES.get(job["status"], job["status"])),
        experiment_id=escape(job["id"]), report_time=escape(now_iso()), content="".join(parts), version=escape(__version__),
    )
    return content.encode("utf-8")
