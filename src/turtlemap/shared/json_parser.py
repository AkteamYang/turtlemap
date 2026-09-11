#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2026 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/04/28
# @Author  : YaHaoo
# @File    : json_parser.py
"""JSON 字符串清洗与容错处理工具。"""

import json

from json_repair import repair_json
from pydantic import BaseModel


def sanitize_json_string(raw_text: str, empty_str: str | None = None) -> str:
    """对模型返回的 JSON 字符串做清洗和容错处理。

    当前处理策略：
    - 去掉首尾空白和 BOM。
    - 去掉 Markdown 代码块包裹。
    - 从混杂文本中提取首个完整 JSON 对象或数组。
    - 先尝试严格 ``json.loads``。
    - 严格解析失败时，使用 ``json_repair`` 进行修复后再校验。

    参数:
        raw_text: 模型返回的原始文本。
        empty_str: 当清洗后为空时的默认值

    返回:
        清洗并通过 JSON 语法校验的字符串。

    异常:
        AgentException: 当输入为空、找不到 JSON 片段，或严格解析与修复后仍无法得到合法 JSON 时抛出。
    """
    normalized_text = raw_text.lstrip("\ufeff").strip()
    if not normalized_text:
        if not empty_str:
            raise RuntimeError("JSON 清洗失败：输入内容为空")
        
        normalized_text = empty_str

    candidate_text = _strip_markdown_code_fence(normalized_text)

    json_fragment = _extract_first_json_fragment(candidate_text)
    if json_fragment is None:
        preview = _truncate_text(candidate_text)
        raise RuntimeError(f"JSON 清洗失败：未找到完整的 JSON 对象或数组，content={preview}")

    try:
        json.loads(json_fragment)
        return json_fragment
    except json.JSONDecodeError as strict_exc:
        try:
            repaired_json = repair_json(
                json_fragment,
                skip_json_loads=True,
                ensure_ascii=False
            )
            json.loads(repaired_json)
            return repaired_json
        except Exception as repair_exc:
            preview = _truncate_text(json_fragment)
            raise RuntimeError(
                "JSON 清洗失败：严格解析失败，且 json_repair 修复后仍不是合法 JSON，"
                f"strict_error=line={strict_exc.lineno}, column={strict_exc.colno}, error={strict_exc.msg}; "
                f"repair_error={repair_exc}; content={preview}"
            ) from repair_exc


def dump_to_static_json(json_dict: dict | BaseModel) -> str:
    if isinstance(json_dict, BaseModel):
        json_dict = json_dict.model_dump(mode="json")
    result = json.dumps(
                json_dict,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
    return result


def _strip_markdown_code_fence(text: str) -> str:
    """去掉 Markdown 代码块包裹。

    参数:
        text: 原始文本。

    返回:
        若文本整体是代码块，则返回代码块内部内容；否则返回原始文本。
    """
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) < 2:
        return text
    if not lines[-1].strip().startswith("```"):
        return text

    return "\n".join(lines[1:-1]).strip()


def _extract_first_json_fragment(text: str) -> str | None:
    """从混杂文本中提取首个完整 JSON 片段。

    参数:
        text: 待扫描文本。

    返回:
        首个完整 JSON 对象或数组字符串；未找到时返回 ``None``。
    """
    start_index = None
    opening_char = ""
    closing_char = ""
    for index, char in enumerate(text):
        if char == "{":
            start_index = index
            opening_char = "{"
            closing_char = "}"
            break
        if char == "[":
            start_index = index
            opening_char = "["
            closing_char = "]"
            break

    if start_index is None:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for index in range(start_index, len(text)):
        char = text[index]

        if escape_next:
            escape_next = False
            continue

        if char == "\\" and in_string:
            escape_next = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == opening_char:
            depth += 1
        elif char == closing_char:
            depth -= 1
            if depth == 0:
                return text[start_index:index + 1].strip()

    return None


def _truncate_text(text: str, limit: int = 200) -> str:
    """截断过长文本，避免错误日志过大。

    参数:
        text: 原始文本。
        limit: 最大保留字符数。

    返回:
        截断后的文本。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"
