#!/usr/bin/env python3
"""把配置模板里**缺失**的键增量补进已有的 config.json。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

===== 为什么需要它 =====
`/data/config.json` 只在**首次启动**时从模板生成，之后不再重新生成（那会覆盖
用户/manager 在设置页里写下的配置）。于是当上游**新增**配置项时，老部署的
config.json 里就没有那个键，表现为界面上一堆莫名其妙的现象。

真实事故（就是写这个文件的原因）：
  上游新增 `pool.cost_explore_interval` 后，老部署的 config.json 里没有它。
  设置页里 `durationOk()` 把 undefined 宽容成空串、判定通过，而 `pickValues()`
  却把**原始**的 undefined 字符串化 —— 于是那个输入框里显示出字面量
  `undefined`，还带红框。不是崩溃，但用户完全看不懂。

===== 设计原则：只补不改 =====
**绝不覆盖已有值**，只添加模板里有、config.json 里没有的键。理由与入口脚本
一贯的原则一致：manager 的设置页会写这个文件，启动时悄悄改用户写下的值比
"缺一个键"危险得多。

具体行为：
  * 递归比较嵌套对象；缺失的整棵子树直接补上；
  * 已有值为 null / 类型不符（比如用户把对象写成了字符串）时**不动它** ——
    那是人的决定，不是我们要修的 bug；
  * 列表（如 schedule.checkin_hours）按叶子处理：缺失就整体补上；
  * **跳过 `api_key`**：模板里那是占位符 REPLACED_AT_FIRST_BOOT，
    补进去等于写了个无效密钥；
  * 只在确实补了东西时才写盘，写前留一份 `config.json.bak`；
  * 写盘失败（只读挂载等）只告警，**不让容器起不来**。

输出：把补进去的键路径打到 stdout（带 [suite] 前缀），可审计 ——
用户能从日志里看到"启动时补了哪几个键"，而不是配置被静默改造。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# 不参与补齐的键：
#   api_key  —— 模板里是占位符 REPLACED_AT_FIRST_BOOT，补进去等于写了个无效密钥
#   _comment —— 那是**我们**写在模板里的内部说明，不是上游的配置项。
#              补进去会让下面那句"补齐 N 个上游新增配置项"变成假话。
SKIP_KEYS = {'api_key', '_comment'}


def merge_missing(template: dict, config: dict) -> list[str]:
    """把 template 里缺失的键补进 config（原地修改），返回补进去的键路径。

    只补不改：已存在的键一律保留原值，哪怕它的类型与模板不同。
    """
    added: list[str] = []

    def walk(tpl: dict, cur: dict, prefix: str) -> None:
        for key, tpl_value in tpl.items():
            if key in SKIP_KEYS:
                continue
            path = f'{prefix}{key}'
            if key not in cur:
                cur[key] = tpl_value
                added.append(path)
                continue
            # 两边都是对象才继续往下走；否则保留用户的值
            if isinstance(tpl_value, dict) and isinstance(cur.get(key), dict):
                walk(tpl_value, cur[key], f'{path}.')

    walk(template, config, '')
    return added


def load(path: Path) -> dict:
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f'{path} 的顶层不是对象')
    return data


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('用法: config_merge.py <模板> <config.json>', file=sys.stderr)
        return 2
    template_path, config_path = Path(argv[1]), Path(argv[2])
    if not template_path.is_file() or not config_path.is_file():
        # 文件不全不是本脚本的职责（入口脚本负责生成/校验），静默放过
        return 0

    try:
        template = load(template_path)
        config = load(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f'[suite][WARN] 无法解析配置，跳过增量补齐：{exc}', file=sys.stderr)
        return 0

    added = merge_missing(template, config)
    if not added:
        return 0

    # 写前留一份，便于用户对照"启动时到底改了什么"
    try:
        shutil.copyfile(config_path, config_path.with_suffix(config_path.suffix + '.bak'))
    except Exception as exc:  # noqa: BLE001
        print(f'[suite][WARN] 备份 {config_path} 失败（继续）：{exc}', file=sys.stderr)

    try:
        # 沿用入口脚本生成时的格式（缩进 2、保留中文、末尾换行）
        body = json.dumps(config, ensure_ascii=False, indent=2) + '\n'
        tmp = config_path.with_suffix(config_path.suffix + '.tmp')
        tmp.write_text(body, encoding='utf-8')
        os.replace(tmp, config_path)
    except Exception as exc:  # noqa: BLE001
        print(
            f'[suite][WARN] 无法写入 {config_path}，配置保持原样（服务仍可启动，'
            f'但设置页可能显示 undefined）：{exc}',
            file=sys.stderr,
        )
        return 0

    print(f'[suite] 已为 {config_path.name} 补齐 {len(added)} 个上游新增配置项：')
    for path in added:
        print(f'[suite]   + {path}')
    print(f'[suite] 原文件已备份为 {config_path.name}.bak（本脚本只补缺失键，不改已有值）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
