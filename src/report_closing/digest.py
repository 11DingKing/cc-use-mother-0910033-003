"""确定性摘要工具：为封账报告提供可复核的输入摘要与分块摘要。

所有摘要都建立在规范化 JSON（键排序、无多余空白、UTF-8）之上，
因此同一份数据在任何时刻、任何机器上都得到相同的字节与摘要。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# 导出文件的分块大小：每块最多容纳的纳入记录数
CHUNK_SIZE = 50

# 参与输入摘要的事件字段：任一字段变化都会改变报告输入摘要
FINGERPRINT_FIELDS = (
    "event_id",
    "volunteer_id",
    "school_id",
    "venue_id",
    "theme",
    "service_date",
    "minutes",
    "rating",
    "state",
    "verified_at",
)


def canonical_bytes(value: Any) -> bytes:
    """把数据序列化为字节级稳定的 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def event_fingerprint(event: dict) -> str:
    """单条服务事件的内容指纹。"""
    payload = {key: event.get(key) for key in FINGERPRINT_FIELDS}
    return sha256_hex(canonical_bytes(payload))


def input_digest(events: Iterable[dict]) -> str:
    """全部纳入记录的输入摘要：与事件顺序无关、对内容敏感。"""
    fingerprints = sorted(event_fingerprint(event) for event in events)
    return sha256_hex(canonical_bytes(fingerprints))


def chunk_digests(events: list[dict], chunk_size: int = CHUNK_SIZE) -> list[dict]:
    """把事件列表按编号排序后分块，逐块计算摘要，便于大体量导出分块校验。"""
    ordered = sorted(events, key=lambda item: item["event_id"])
    chunks = []
    for start in range(0, len(ordered), chunk_size):
        part = ordered[start : start + chunk_size]
        chunks.append(
            {
                "index": start // chunk_size,
                "count": len(part),
                "first_event_id": part[0]["event_id"],
                "last_event_id": part[-1]["event_id"],
                "digest": sha256_hex(canonical_bytes([event_fingerprint(event) for event in part])),
            }
        )
    return chunks
