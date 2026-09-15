"""按文档原始字节缓存外部解析产物，避免重复计费。

为什么需要
----------
LlamaParse 按页计费，单份 3~4 页文档实测约 27 秒。而入库链路上有两处会重复
调用解析：

* ``KnowledgeIngestionService._reuse_duplicate_version``：版本未激活时**会重跑
  整条 pipeline**（含 loader），即重新解析一次；
* job 失败重试：用户重新上传同一份文件时再次解析。

缓存键为什么用「原始字节」而不是 ``content_hash``
------------------------------------------------
``DocumentVersion.content_hash`` 的定义是 ``sha256(normalized_text)``，
**必须先解析才能算出来**，救不了这次调用。所以这里对上传的原始字节做 SHA-256。

缓存内容为什么是「归一化前」的产物
----------------------------------
归一化（HTML 表 → 管道表）是纯函数，缓存它的输入可以让归一化规则升级时
不必重新解析、重新计费。

缓存键为什么还要带上档位与版本
------------------------------
同一份字节在 ``fast`` 与 ``agentic`` 档位下的产出不同，``latest`` 也会随
服务端解析器升级而变化。只按字节做键会让「改了档位却读到旧档位结果」这种
静默错误发生，因此键里包含 ``tier`` 与 ``version``。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

# 缓存文件后缀：内容是纯文本 Markdown，便于人工排查。
_CACHE_SUFFIX = ".md"


def cache_key(content: bytes, *, tier: str, version: str) -> str:
    """计算缓存键：内容 + 档位 + 版本三者一起摘要。"""
    digest = hashlib.sha256()
    digest.update(tier.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(content)
    return digest.hexdigest()


class LlamaParseCache:
    """文件系统缓存：``<root>/<前两位>/<完整摘要>.md``。

    按摘要前两位分片，避免单目录下堆积过多文件。写入使用「临时文件 +
    ``os.replace``」的原子替换，确保进程中断不会留下截断的缓存条目——
    那会让后续的读取静默拿到半份文档。
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """缓存根目录，供运维排查与清理。"""
        return self._root

    def path_for(self, key: str) -> Path:
        """返回某个缓存键对应的文件路径。"""
        return self._root / key[:2] / f"{key}{_CACHE_SUFFIX}"

    def get(self, key: str) -> str | None:
        """读取缓存；未命中或内容为空时返回 ``None``。

        读取失败（权限、编码异常等）一律按未命中处理：缓存只是优化，
        它的任何问题都不应该让一次本可成功的上传失败。
        """
        path = self.path_for(key)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return text if text.strip() else None

    def put(self, key: str, markdown: str) -> None:
        """写入缓存；任何 I/O 失败都静默忽略（缓存不该让上传失败）。"""
        if not markdown.strip():
            return
        path = self.path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{key}.",
                suffix=".tmp",
                delete=False,
            )
            try:
                with handle:
                    handle.write(markdown)
                os.replace(handle.name, path)
            except BaseException:
                # 写入中途失败：清掉临时文件，不要留下垃圾。
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError:
            return
