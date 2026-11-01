#!/usr/bin/env python
"""CDP Monitor 入口 - 双击运行此文件"""
import sys
import os

# 确保 src 在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.main import main

if __name__ == "__main__":
    main()
