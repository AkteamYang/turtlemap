#!/usr/bin/python3
# coding: utf-8
#
# Copyright (C) 2025 - 2026 YaHaoo, Inc. All Rights Reserved
#
# @Time    : 2026/06/17 13:26
# @Author  : YaHaoo
# @File    : logger.py

# 导入日志库
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os

from sympy import false


class JsonFormatter(logging.Formatter):
    """将日志序列化为单行 JSON，便于日志采集系统处理多行文本。"""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload = {
            "log_time": self.formatTime(record, self.datefmt),
            "log_level": record.levelname,
            "log_content": message,
            "pid": record.process,
            "module_name": record.name,
            "file_line": f"{record.filename}:{record.lineno}",
            "file_name": record.filename,
            "line_no": record.lineno,
            "function_name": record.funcName,
            "thread_name": record.threadName,
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False)


def _build_formatter(use_json: bool) -> logging.Formatter:
    if use_json:
        return JsonFormatter()
    return logging.Formatter(
        '[%(asctime)s] [PID:%(process)d] - %(levelname)s- %(filename)s:%(lineno)d - %(funcName)s() - %(message)s'
    )


def setup_logger(log_dir: str = "", file_name: str = 'ecomind_log') -> logging.Logger:
    """
    创建日志对象
    :param log_dir: 日志目录，xxx/log
    :param file_name: 例如：pipeline，则生成的日志文件为：pipeline.log, pipeline_err.log
    :return:
    """
    # 获取日志器
    logger = logging.getLogger(file_name)
    # 设置日志级别
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不向父日志器传播日志
    # print(f'logger.handlers-->{logger.handlers}')
    # 避免重复添加处理器
    if not logger.handlers:
        # 设置日志格式
        formatter = _build_formatter(false)
        # 创建控制台处理器
        console_handler = logging.StreamHandler()
        # 设置控制台处理器级别
        console_handler.setLevel(logger.level)
        # 为控制台处理器设置格式
        console_handler.setFormatter(formatter)
        # # 添加控制台处理器
        logger.addHandler(console_handler)

        # error日志
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            err_log_file = os.path.join(log_dir, f'{file_name}_err.log')

            # 文件formatter
            file_formatter = _build_formatter(False)

            # 创建文件处理器
            error_file_handler = TimedRotatingFileHandler(
                err_log_file,
                when="midnight",
                interval=1,
                backupCount=30,
                encoding="utf-8"
            )
            error_file_handler.setLevel(logging.WARNING)
            # 为文件处理器设置格式
            error_file_handler.setFormatter(file_formatter)
            # 添加文件处理器
            logger.addHandler(error_file_handler)

            # 普通日志
            log_file = os.path.join(log_dir, f'{file_name}.log')
            # 创建文件处理器
            file_handler = TimedRotatingFileHandler(
                log_file,
                when="midnight",
                interval=1,
                backupCount=15,
                encoding="utf-8"
            )
            file_handler.setLevel(logger.level)
            # 为文件处理器设置格式
            file_handler.setFormatter(file_formatter)
            # 添加文件处理器
            logger.addHandler(file_handler)
    return logger


class Logger:
    logger = setup_logger()

    @classmethod
    def set_logger(cls, logger: logging.Logger):
        cls.logger = logger

