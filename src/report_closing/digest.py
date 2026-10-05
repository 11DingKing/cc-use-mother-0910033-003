"""确定性摘要工具：规范化 JSON 与 SHA-256。"""
from __future__ import annotations

import hashlib
import json


def canonical_json(value: object) -> str:
    """生成键序稳定、无空白的 JSON 文本，保证重复序列化结果一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_digest(value: object) -> str:
    """任意可 JSON 序列化对象的内容摘要。"""
    return sha256_hex(canonical_json(value))
