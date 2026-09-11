#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/07/11 12:32
# @Author  : YaHaoo
# @File    : polymorphic.py

"""kernel 层 pydantic 多态状态模型支持。"""

from __future__ import annotations

from enum import Enum
from typing import Callable, ClassVar, TypeVar, cast

from pydantic import BaseModel, ConfigDict

from ..exceptions import KernelRuntimeError

PolymorphicModelT = TypeVar("PolymorphicModelT", bound="PolymorphicStateModel")
SelfModel = TypeVar("SelfModel", bound=BaseModel)

class BaseStateModel(BaseModel):
    """表示可持久化状态模型的统一 pydantic 基类。

    说明:
        该基类用于需要序列化、反序列化和 checkpoint 恢复的状态对象。
        运行期服务、协议实现和生命周期对象不应因为复用该基类而被状态化。
    """

    # 统一允许少量运行期复杂对象字段，禁止额外字段静默进入状态快照。
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        # extra="forbid",
        populate_by_name=True,
    )


class PolymorphicStateModel(BaseStateModel):
    """表示支持 type_name 注册恢复的多态状态模型基类。

    说明:
        子类通过 `@Base.register_type` 注册到父类注册表，注册 key 来自子类声明的 `type_name` 默认值。
        反序列化时父类根据 `type_name` 找到真实子类，再交给 pydantic
        完成字段校验和对象构造。
    """

    # 当前对象的多态类型名，用于持久化恢复时选择真实子类。
    type_name: str

    # 当前多态基类维护的类型名到子类的映射表。
    _type_name2class: ClassVar[dict[str, type["PolymorphicStateModel"]]] = {}

    @classmethod
    def register_type(cls, subclass: type[PolymorphicModelT]) -> type[PolymorphicModelT]:
        """注册一个多态子类。

        参数:
            subclass: 当前被装饰的多态子类，必须声明稳定的 `type_name` 默认值。

        返回:
            原始子类，便于正常完成类定义。
        """

        type_name = cls._read_subclass_type_name(subclass)
        registry = cls._get_registry()
        existing_class = registry.get(type_name)
        if existing_class is not None and existing_class is not subclass:
            raise KernelRuntimeError(
                f"多态状态类型重复注册：type_name={type_name}"
            )
        registry[type_name] = subclass
        return subclass

    @classmethod
    def validate_polymorphic(
        cls: type[PolymorphicModelT],
        value: dict[str, object],
    ) -> PolymorphicModelT:
        """按 `type_name` 把输入恢复为真实多态子类。

        参数:
            value: 当前待恢复的 JSON object 字典。

        返回:
            已恢复的真实多态子类实例。
        """

        raw_type_name = value.get("type_name")
        if not isinstance(raw_type_name, str):
            raise KernelRuntimeError(
                f"多态状态恢复失败：缺少 type_name，base={cls.__name__}"
            )

        model_class = cls._get_registry().get(raw_type_name)
        if model_class is None:
            raise KernelRuntimeError(
                f"多态状态恢复失败：未注册的 type_name={raw_type_name}，base={cls.__name__}"
            )

        return cast(PolymorphicModelT, model_class.model_validate(value))


    @classmethod
    def _read_subclass_type_name(cls, subclass: type[PolymorphicModelT]) -> str:
        """读取子类声明的稳定 type_name 默认值。

        参数:
            subclass: 当前准备注册的多态子类。

        返回:
            子类字段 `type_name` 上声明的非空默认字符串。
        """

        type_name_field = subclass.model_fields.get("type_name")
        if type_name_field is None or not isinstance(type_name_field.default, str) or not type_name_field.default:
            raise KernelRuntimeError(
                f"多态状态类型注册失败：子类未声明有效 type_name 默认值，class={subclass.__name__}"
            )
        return type_name_field.default

    @classmethod
    def _get_registry(cls) -> dict[str, type["PolymorphicStateModel"]]:
        """获取当前多态基类自己的注册表。

        返回:
            当前类绑定的类型名到子类映射表。
        """

        if "_type_name2class" not in cls.__dict__:
            cls._type_name2class = {}
        return cls._type_name2class


class ValidateFieldModel(BaseStateModel):
    """表示可按类型字段恢复未知字段真实模型的基类。

    说明:
        部分状态字段在 kernel 中只能声明为 `Any` 或较宽的 union，例如
        `RuntimeArtifact.payload`。子类可通过 `register_field_type(...)`
        注册“字段名 + 类型名 -> 模型类”的映射，再在字段 validator 中调用
        `validate_field(...)` 恢复真实模型。
    """

    # 字段名到类型名再到模型类的映射，用于反序列化宽类型字段。
    _field_name2type_name2class: ClassVar[dict[str, dict[str, type[BaseModel]]]] = {}

    @classmethod
    def register_field_type(
        cls,
        field_name: str,
        type_name: str | Enum,
    ) -> Callable[[type[SelfModel]], type[SelfModel]]:
        """注册指定字段在某个类型值下对应的模型类。

        参数:
            field_name: 需要恢复真实类型的字段名。
            type_name: 外层对象中的类型值，枚举会使用其 `value` 作为注册键。

        返回:
            可用于装饰目标字段模型类的注册函数。
        """

        normalized_type_name = cls._normalize_type_name(type_name)
        if not field_name:
            raise KernelRuntimeError(
                f"字段类型注册失败：field_name 不能为空，base={cls.__name__}"
            )

        def wrapped(subclass: type[SelfModel]) -> type[SelfModel]:
            """登记字段模型类并返回原始类。

            参数:
                subclass: 当前待注册的字段模型类。

            返回:
                原始字段模型类，便于装饰器链继续正常工作。
            """

            field_registry = cls._get_field_type_registry().setdefault(field_name, {})
            existing_class = field_registry.get(normalized_type_name)
            if existing_class is not None and existing_class is not subclass:
                raise KernelRuntimeError(
                    "字段类型重复注册："
                    f"base={cls.__name__}, field={field_name}, type_name={normalized_type_name}"
                )

            field_registry[normalized_type_name] = subclass
            return subclass

        return wrapped

    @classmethod
    def validate_field(
        cls,
        field_name: str,
        type_name: str | Enum | None,
        value: object,
    ) -> object:
        """根据字段名和类型值恢复字段真实模型。

        参数:
            field_name: 当前正在恢复的字段名。
            type_name: 外层对象中用于选择字段类型的类型值。
            value: Pydantic 解析前的字段原始值。

        返回:
            若存在注册模型且原始值为 dict，则返回对应模型实例；否则返回原值。
        """

        if type_name is None or not isinstance(value, dict):
            return value

        type_name2class = cls._get_field_type_registry().get(field_name)
        if type_name2class is None:
            return value

        normalized_type_name = cls._normalize_type_name(type_name)
        field_class = type_name2class.get(normalized_type_name)
        if field_class is None:
            return value

        # 只有注册字段进入强类型恢复，未注册字段保持宽类型原样透传。
        return field_class.model_validate(value)

    @classmethod
    def _normalize_type_name(cls, type_name: str | Enum) -> str:
        """把注册或解析时的类型值统一转换为字符串键。

        参数:
            type_name: 字符串或枚举形式的类型值。

        返回:
            可用于字段类型注册表的字符串键。
        """

        if isinstance(type_name, Enum):
            normalized_type_name = str(type_name.value)
        else:
            normalized_type_name = str(type_name)
        if not normalized_type_name:
            raise KernelRuntimeError(
                f"字段类型注册失败：type_name 不能为空，base={cls.__name__}"
            )
        return normalized_type_name

    @classmethod
    def _get_field_type_registry(cls) -> dict[str, dict[str, type[BaseModel]]]:
        """获取当前模型类自己的字段类型注册表。

        返回:
            当前类绑定的字段类型恢复映射表。
        """

        if "_field_name2type_name2class" not in cls.__dict__:
            cls._field_name2type_name2class = {}
        return cls._field_name2type_name2class
