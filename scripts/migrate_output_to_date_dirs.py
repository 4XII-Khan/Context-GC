#!/usr/bin/env python3
"""
将 tests/output/ 根目录下的旧输出文件迁移到对应日期目录。

用法: python3 scripts/migrate_output_to_date_dirs.py
"""
import os
import shutil
from pathlib import Path
from datetime import datetime

OUTPUT_BASE = Path(__file__).parent.parent / "tests" / "output"

# 根目录下需迁移的文件/目录 -> 根据 mtime 推断的日期
MIGRATE_ITEMS = [
    "e2e_test_report.txt",
    "e2e_test_data",
    "test_100_rounds_log.txt",
    "test_100_rounds_final_context.txt",
    "test_100_rounds_evaluation.md",
]


def main():
    for name in MIGRATE_ITEMS:
        src = OUTPUT_BASE / name
        if not src.exists():
            continue
        mtime = os.path.getmtime(src)
        date_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        dest_dir = OUTPUT_BASE / date_str
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists():
            # 目标已存在（日期目录已有更新输出），删除根目录重复文件
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
            print(f"清理重复: {name}（已存在于 {date_str}/）")
            continue
        shutil.move(str(src), str(dest))
        print(f"迁移: {name} -> {date_str}/")


if __name__ == "__main__":
    main()
