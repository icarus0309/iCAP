"""
日志模块 - 记录程序运行日志
"""
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional


class Logger:
    """统一日志管理器"""
    
    _instance: Optional['Logger'] = None
    
    def __init__(self, name: str = "SkillTuning", log_dir: str = "logs"):
        self.name = name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        
        if not self.logger.handlers:
            self._setup_handlers()
    
    def _setup_handlers(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        file_handler = logging.FileHandler(
            self.log_dir / f"{self.name}_{timestamp}.log",
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
    
    @classmethod
    def get_instance(cls, name: str = "SkillTuning", log_dir: str = "logs") -> 'Logger':
        if cls._instance is None:
            cls._instance = cls(name, log_dir)
        return cls._instance
    
    def debug(self, msg: str):
        self.logger.debug(msg)
    
    def info(self, msg: str):
        self.logger.info(msg)
    
    def warning(self, msg: str):
        self.logger.warning(msg)
    
    def error(self, msg: str):
        self.logger.error(msg)
    
    def critical(self, msg: str):
        self.logger.critical(msg)
    
    def section(self, title: str):
        separator = "=" * 60
        self.info(separator)
        self.info(title)
        self.info(separator)
    
    def subsection(self, title: str):
        separator = "-" * 40
        self.info(separator)
        self.info(title)
        self.info(separator)


def get_logger(name: str = "SkillTuning", log_dir: str = "logs") -> Logger:
    """获取日志管理器实例"""
    return Logger.get_instance(name, log_dir)