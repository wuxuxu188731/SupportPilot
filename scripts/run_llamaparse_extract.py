"""用 LlamaParse（llama-cloud）把富文本文档提取成 Markdown / 纯文本。

背景
----
现有 ``app.knowledge.document_loader`` 只能本地解析 Markdown/TXT（UTF-8 解码）
以及 DOCX（python-docx 直读 OOXML）。PDF 完全无法处理，DOCX 也只能拿回
"一整块无标题层级的正文"。本脚本是 loader 改造前的能力验证工具：把同一份
文档交给 LlamaParse 解析，导出它返回的 Markdown、纯文本与逐页元数据，供
``scripts/evaluate_llamaparse_extraction.py`` 做保真度与 RAG 适配性评估。

设计要点
--------
* **幂等优先**：每个输入文件对应一个输出子目录，默认已存在产物就跳过解析，
  避免重复消耗 LlamaParse 额度；需要重跑时显式传 ``--force``。
* **密钥只走环境变量**：从 ``LLAMA_CLOUD_API_KEY`` 读取（可写在被 git 忽略的
  ``.env`` 里），脚本本身不落任何明文密钥。
* **失败即失败**：任何上传/解析异常都向上抛出并以非 0 退出码结束，不把失败
  伪装成"空文档"，否则下游评估会把解析失败误判成"内容为空"。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 脚本引导：``python scripts/xxx.py`` 只会把 scripts/ 放进 sys.path，
# 这里补上仓库根目录，保证 ``app`` 包能被导入。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from typing import Sequence

from dotenv import load_dotenv

# 默认输出目录：.artifacts/ 已在 .gitignore 中，产物属于可再生数据，不入库。
DEFAULT_OUTPUT_DIR = _REPO_ROOT / ".artifacts" / "llamaparse-extract"
# 默认解析档位：agentic 是 LlamaParse 面向复杂版式的高质量档位，
# 与仓库内 TestLlamaParse.py 已验证可用的档位保持一致。
DEFAULT_TIER = "agentic"
DEFAULT_VERSION = "latest"
# 支持的扩展名 -> 用于选择解析策略；LlamaParse 本身支持更多格式，
# 这里只登记本轮 loader 改造关心的几种。
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".html", ".md", ".txt"}


@dataclass
class PageInfo:
    """LlamaParse 逐页解析结果。

    ``markdown``/``text`` 是该页的两种视图；``confidence`` 是解析置信度，
    取值为 0~1，越低说明版式识别越可能出错，是判断"能否直接入库"的
    重要信号。
    """

    page_number: int          # 页码，从 1 开始，与原始文档页码一致
    markdown: str             # 该页的 Markdown 视图（保留标题与表格结构）
    text: str                 # 该页的纯文本视图（无任何排版标记）
    confidence: float | None  # 该页解析置信度（0~1）；服务端未返回时为 None


@dataclass
class ParsedDocument:
    """一个输入文件的完整解析产物。"""

    source_path: str          # 原始文件绝对路径
    source_name: str          # 原始文件名（含扩展名）
    source_suffix: str        # 原始文件扩展名（小写，含点）
    source_bytes: int         # 原始文件字节数
    file_id: str              # LlamaParse 服务端文件 id，便于回溯与复查
    job_id: str | None        # 解析任务 id；同步接口未返回时为 None
    tier: str                 # 实际使用的解析档位
    version: str              # 实际使用的解析器版本
    elapsed_seconds: float    # 端到端耗时（含上传与轮询等待）
    pages: list[PageInfo] = field(default_factory=list)  # 逐页结果

    @property
    def markdown(self) -> str:
        """整篇文档的 Markdown：按页拼接，页与页之间保留一个空行。"""
        return "\n\n".join(page.markdown for page in self.pages).strip() + "\n"

    @property
    def text(self) -> str:
        """整篇文档的纯文本：按页拼接。"""
        return "\n\n".join(page.text for page in self.pages).strip() + "\n"


def _read_api_key() -> str:
    """读取 LlamaParse 密钥；缺失时给出可操作的报错而不是空字符串。"""
    load_dotenv(_REPO_ROOT / ".env")
    api_key = os.getenv("LLAMA_CLOUD_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "LLAMA_CLOUD_API_KEY is required: 请在 .env 或环境变量中配置 LlamaParse 密钥"
        )
    return api_key


def parse_document(
    path: Path,
    *,
    api_key: str,
    tier: str = DEFAULT_TIER,
    version: str = DEFAULT_VERSION,
    timeout: float = 1800.0,
    verbose: bool = False,
) -> ParsedDocument:
    """把单个本地文件交给 LlamaParse 解析并返回结构化产物。

    流程分两步：先 ``files.create`` 上传并拿到服务端 ``file_id``；再调用
    ``parsing.parse`` 同步等待解析完成（内部自带轮询），一次性取回
    markdown / text / metadata 三种视图。
    """
    # 延迟导入：让 ``--help`` 等纯参数操作在没有安装 llama-cloud 时也能正常工作。
    from llama_cloud import LlamaCloud

    client = LlamaCloud(api_key=api_key)

    started_at = time.monotonic()
    uploaded = client.files.create(file=str(path), purpose="parse")
    result = client.parsing.parse(
        file_id=uploaded.id,
        tier=tier,
        version=version,
        expand=["markdown", "text", "metadata"],
        timeout=timeout,
        verbose=verbose,
    )
    elapsed = time.monotonic() - started_at

    # 逐页对齐三种视图。服务端以 page_number 标识页序，这里以 markdown 视图
    # 的页为准，缺失的 text/metadata 页用空串/None 兜底，保证页数不漂移。
    markdown_pages = list(getattr(result.markdown, "pages", []) or []) if result.markdown else []
    text_pages = list(getattr(result.text, "pages", []) or []) if result.text else []
    meta_pages = list(getattr(result.metadata, "pages", []) or []) if result.metadata else []

    text_by_number = {getattr(page, "page_number", 0): page for page in text_pages}
    meta_by_number = {getattr(page, "page_number", 0): page for page in meta_pages}

    pages: list[PageInfo] = []
    for index, md_page in enumerate(markdown_pages, start=1):
        page_number = getattr(md_page, "page_number", index) or index
        text_page = text_by_number.get(page_number)
        meta_page = meta_by_number.get(page_number)
        pages.append(
            PageInfo(
                page_number=page_number,
                markdown=getattr(md_page, "markdown", "") or "",
                text=(getattr(text_page, "text", "") or "") if text_page else "",
                confidence=(
                    getattr(meta_page, "confidence", None) if meta_page else None
                ),
            )
        )

    return ParsedDocument(
        source_path=str(path.resolve()),
        source_name=path.name,
        source_suffix=path.suffix.lower(),
        source_bytes=path.stat().st_size,
        file_id=str(uploaded.id),
        job_id=str(getattr(result, "job_id", "") or "") or None,
        tier=tier,
        version=version,
        elapsed_seconds=round(elapsed, 2),
        pages=pages,
    )


def _output_dir_for(output_root: Path, source: Path) -> Path:
    """为输入文件分配输出子目录；同名文件用扩展名区分，避免互相覆盖。"""
    return output_root / f"{source.stem}{source.suffix.lower()}"


def write_artifacts(parsed: ParsedDocument, output_root: Path) -> Path:
    """把解析产物落盘，返回该文件的输出目录。"""
    target = _output_dir_for(output_root, Path(parsed.source_path))
    target.mkdir(parents=True, exist_ok=True)

    (target / "markdown.md").write_text(parsed.markdown, encoding="utf-8")
    (target / "text.txt").write_text(parsed.text, encoding="utf-8")
    (target / "pages.json").write_text(
        json.dumps(
            [
                {
                    "page_number": page.page_number,
                    "confidence": page.confidence,
                    "markdown_chars": len(page.markdown),
                    "text_chars": len(page.text),
                }
                for page in parsed.pages
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (target / "meta.json").write_text(
        json.dumps(
            {
                "source_path": parsed.source_path,
                "source_name": parsed.source_name,
                "source_suffix": parsed.source_suffix,
                "source_bytes": parsed.source_bytes,
                "file_id": parsed.file_id,
                "job_id": parsed.job_id,
                "tier": parsed.tier,
                "version": parsed.version,
                "elapsed_seconds": parsed.elapsed_seconds,
                "page_count": len(parsed.pages),
                "markdown_chars": len(parsed.markdown),
                "text_chars": len(parsed.text),
                "min_confidence": min(
                    (p.confidence for p in parsed.pages if p.confidence is not None),
                    default=None,
                ),
                "mean_confidence": (
                    round(
                        sum(p.confidence for p in parsed.pages if p.confidence is not None)
                        / max(1, sum(1 for p in parsed.pages if p.confidence is not None)),
                        4,
                    )
                    if any(p.confidence is not None for p in parsed.pages)
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def collect_inputs(raw_inputs: Sequence[str]) -> list[Path]:
    """把命令行传入的文件/目录展开成待解析文件列表（去重且保持稳定顺序）。"""
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_inputs:
        candidate = Path(raw)
        if not candidate.exists():
            raise FileNotFoundError(f"输入路径不存在：{candidate}")
        candidates = (
            sorted(p for p in candidate.iterdir() if p.is_file())
            if candidate.is_dir()
            else [candidate]
        )
        for item in candidates:
            resolved = item.resolve()
            if resolved in seen or resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            seen.add(resolved)
            files.append(resolved)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用 LlamaParse 把 docx/pdf 等文档提取成 Markdown 与纯文本"
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        required=True,
        help="输入文件或目录，可重复传入",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"产物输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--tier",
        default=DEFAULT_TIER,
        help="LlamaParse 解析档位：fast / cost_effective / agentic / agentic_plus",
    )
    parser.add_argument("--version", default=DEFAULT_VERSION, help="解析器版本")
    parser.add_argument(
        "--timeout", type=float, default=1800.0, help="单文件解析超时（秒）"
    )
    parser.add_argument(
        "--force", action="store_true", help="已存在产物时强制重新解析（会消耗额度）"
    )
    parser.add_argument("--verbose", action="store_true", help="打印解析进度")
    args = parser.parse_args(argv)

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    api_key = _read_api_key()

    targets = collect_inputs(args.inputs)
    if not targets:
        print("没有找到可解析的文件", file=sys.stderr)
        return 1

    manifest: list[dict[str, object]] = []
    for source in targets:
        target_dir = _output_dir_for(output_root, source)
        if (target_dir / "markdown.md").exists() and not args.force:
            print(f"[跳过] {source.name}：产物已存在（--force 可强制重跑）")
            manifest.append({"source_name": source.name, "status": "skipped"})
            continue

        print(f"[解析] {source.name}（tier={args.tier}）...")
        parsed = parse_document(
            source,
            api_key=api_key,
            tier=args.tier,
            version=args.version,
            timeout=args.timeout,
            verbose=args.verbose,
        )
        written_to = write_artifacts(parsed, output_root)
        print(
            f"[完成] {source.name} 页数={len(parsed.pages)} "
            f"markdown字符={len(parsed.markdown)} 耗时={parsed.elapsed_seconds}s "
            f"-> {written_to}"
        )
        manifest.append(
            {
                "source_name": source.name,
                "status": "parsed",
                "page_count": len(parsed.pages),
                "markdown_chars": len(parsed.markdown),
                "elapsed_seconds": parsed.elapsed_seconds,
                "file_id": parsed.file_id,
                "output_dir": str(written_to),
            }
        )

    (output_root / "manifest.json").write_text(
        json.dumps(
            {"tier": args.tier, "version": args.version, "items": manifest},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
