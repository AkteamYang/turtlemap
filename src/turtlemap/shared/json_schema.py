#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2026 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/04/28
# @Author  : YaHaoo
# @File    : json_schema.py
"""Pydantic JSON Schema 处理工具。"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel


def pydantic_model_to_json_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """把 Pydantic 模型类转换为可直接下发给模型的 JSON Schema。

    当前处理策略：
    - 输入必须是 ``BaseModel`` 子类。
    - 递归移除 ``title``，减少无效噪音。
    - 展开本地 ``$defs / $ref`` 引用，避免部分模型服务不支持引用语法。
    - 最终移除根层 ``$defs``，只保留展开后的对象结构。

    参数:
        model_cls: 目标 Pydantic 模型类。

    返回:
        展开引用并去掉冗余字段后的 JSON Schema 字典。

    异常:
        RuntimeError: 当输入不是 Pydantic 模型类，或 schema 引用无法解析时抛出。
    """
    if not isinstance(model_cls, type) or not issubclass(model_cls, BaseModel):
        raise RuntimeError("model_cls 必须是 Pydantic BaseModel 子类")

    schema = deepcopy(model_cls.model_json_schema(mode="validation"))
    definitions = deepcopy(schema.get("$defs", {}))
    schema = _sanitize_schema(schema)
    expanded_schema = _expand_schema_references(schema, definitions, ref_stack=[])
    expanded_schema.pop("$defs", None)
    return expanded_schema


def _sanitize_schema(schema: Any) -> dict:
    """原地移除 schema 中对模型约束无帮助的冗余字段。

    参数:
        schema: 待处理的 schema 对象，可以是字典、列表或标量。
    """
    """
    model对应的json schema主要是参数描述
    {
      "type": "object",
      "properties": {
        "city": {
          "type": "string",
          "description": "城市名称，例如上海"
        },
        "date": {
          "type": "string",
          "description": "查询日期，格式 YYYY-MM-DD"
        }
      },
      "required": ["city"]
    }
    """
    fixed_schema = {
        k: v for k, v in schema.items() if k in ["type", "properties", "required"]
    }
    fixed_schema["properties"] = {
        k: {
            kk: vv for kk, vv in v.items() if kk in ["type", "description"]
        } for k, v in fixed_schema["properties"].items()
    }
    return fixed_schema


def _expand_schema_references(
    schema: Any,
    definitions: dict[str, Any],
    ref_stack: list[str]
) -> Any:
    """递归展开 schema 中的本地 ``$ref`` 引用。

    参数:
        schema: 当前待展开的 schema 片段。
        definitions: 根 schema 中的 ``$defs`` 定义映射。
        ref_stack: 当前展开链路上的引用栈，用于检测循环引用。

    返回:
        引用展开后的 schema 片段。

    异常:
        AgentException: 当引用不存在，或出现循环引用时抛出。
    """
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str):
            definition_name = _parse_local_ref_name(ref)
            if definition_name in ref_stack:
                raise RuntimeError(f"检测到循环 schema 引用，ref={ref}")
            if definition_name not in definitions:
                raise RuntimeError(f"无法解析 schema 引用，ref={ref}")

            expanded_definition = _expand_schema_references(
                deepcopy(definitions[definition_name]),
                definitions,
                ref_stack=ref_stack + [definition_name]
            )

            extra_constraints = {
                key: _expand_schema_references(value, definitions, ref_stack)
                for key, value in schema.items()
                if key != "$ref"
            }
            if extra_constraints:
                if not isinstance(expanded_definition, dict):
                    raise RuntimeError(f"schema 引用展开结果非法，ref={ref}")
                expanded_definition.update(extra_constraints)
            return expanded_definition

        return {
            key: _expand_schema_references(value, definitions, ref_stack)
            for key, value in schema.items()
            if key != "$defs"
        }

    if isinstance(schema, list):
        return [
            _expand_schema_references(item, definitions, ref_stack)
            for item in schema
        ]

    return schema


def _parse_local_ref_name(ref: str) -> str:
    """从本地 ``$ref`` 中提取定义名。

    参数:
        ref: JSON Schema 本地引用路径。

    返回:
        ``$defs`` 中对应的定义名称。

    异常:
        AgentException: 当引用不是 ``#/$defs/<name>`` 形式时抛出。
    """
    ref_prefix = "#/$defs/"
    if not ref.startswith(ref_prefix):
        raise RuntimeError(f"当前仅支持本地 $defs 引用，ref={ref}")
    return ref[len(ref_prefix):]
