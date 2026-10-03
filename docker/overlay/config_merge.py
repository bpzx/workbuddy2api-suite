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

===== 设计原则：只补"缺"、只删"死"，绝不改"值" =====
两个方向都以**模板**为判据，而模板的键集由 `tests/test_config_template.py`
强制与上游 `config.example.json` 逐路径一致 —— 所以：

  * 模板有、config 没有 → 上游**新增**的项，补上（否则界面出现 undefined）；
  * 模板没有、config 有   → 上游**已移除**的死键，删掉（理由见下）。

**已有值一律不动**（包括 null 与类型不符的情况）：manager 的设置页会写这个
文件，启动时悄悄改用户写下的值，比"少一个键"危险得多。

===== 为什么"死键"也要清（第二个真实事故）=====
`server.max_body_mb: 8` 是上游早已移除的键（请求体现在**无上限**，见
`internal/server/handler.go` 的注释）。它不生效，但**它会说话**：排查 502 / 413
时看到它，会以为还存在一个 8MB 的请求体上限，被引到完全错误的方向 —— 这与开头
那个 `undefined` 事故是同一类问题：**配置在描述一个并不存在的行为**。

===== 值一律不动 =====
本脚本只增删**键**，绝不改写**值**。分工是这样：
  * **键集**由模板负责（上游认哪些键、叫什么名字）—— 这是本脚本的事；
  * **值**属于部署自己的数据：`config.json` 在 `./data` 里、跨重新部署保留，
    设置页写的也是它。拿模板去覆盖，既会退回你在面板里做过的调整，
    也会抹掉模板里**给不出**的东西（api_key 是占位符、device_token 是 OAuth
    拿到的凭据、upstash 是你自己申请的）。

具体行为：
  * 递归比较嵌套对象；缺失的整棵子树直接补上；
  * 死键**逐个叶子**清理并逐条打日志（不把整段糊成一行，便于对照原值）；
    清空后只剩空壳的分支（如只含 `max_body_mb` 的 `server` 段）一并摘掉；
  * 已有值为 null / 类型不符（比如用户把对象写成了字符串）时**不动它**；
  * 列表（如 schedule.checkin_hours）按叶子处理：缺失就整体补上；
  * **`api_key` / `_comment` 既不补也不删**：前者模板里是占位符
    REPLACED_AT_FIRST_BOOT（补进去等于写了个无效密钥），后者是我们写在模板里的
    内部说明、不是上游配置项；
  * 死键的原值打进日志便于对照，但**键名含 key / token / secret 等字样的一律隐去**；
  * 只在确实有增删时才写盘，写前留一份 `config.json.bak`；
  * 写盘失败（只读挂载等）只告警，**不让容器起不来**。

输出：把补进 / 清掉的键路径打到 stdout（带 [suite] 前缀），可审计 ——
用户能从日志里看到"启动时改了什么"，而不是配置被静默改造。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# 两个方向都不碰的键：
#   api_key  —— 模板里是占位符 REPLACED_AT_FIRST_BOOT，补进去等于写了个无效密钥；
#               它同时是**凭据**，清理时更不该动（模板里有它，本来也不会被清）。
#   _comment —— 那是**我们**写在模板里的内部说明，不是上游的配置项。
#               补进去会让下面那句"补齐 N 个上游新增配置项"变成假话。
SKIP_KEYS = {'api_key', '_comment'}

# 打印死键原值时的隐去规则：键名含这些字样的一律不打印值（只打键名）。
# 为什么要有：清理日志会把"这个键原来是什么值"打出来便于对照，
# 但配置里可能有 `upstream.device_token`、`upstash.token` 这类凭据 ——
# 哪怕当前模板里它们确实存在（不会被清），也不给"将来某天"留隐患。
SENSITIVE_HINTS = ('key', 'token', 'secret', 'password', 'passwd', 'credential', 'cookie')


def _safe_value(key: str, value: object) -> str:
    """死键原值的可读表示：敏感键隐去、超长截断。"""
    low = key.lower()
    if any(hint in low for hint in SENSITIVE_HINTS):
        return '<已隐去>'
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        text = repr(value)
    return text if len(text) <= 60 else text[:57] + '...'


def merge_missing(template: dict, config: dict) -> list[str]:
    """把 template 里缺失的键补进 config（原地修改），返回补进去的键路径。

    只补"缺"、只改"键集"：已存在的键一律保留原值，哪怕它的类型与模板不同。
    （清掉上游已移除的死键是 `prune_removed` 的职责，方向相反、判据同一个模板。）
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


def prune_removed(template: dict, config: dict) -> list[tuple[str, str]]:
    """把模板里**没有**的键从 config 删掉（原地修改），返回 [(键路径, 原值表示)]。

    只删键、不改任何值。判据是模板；模板的键集等于上游 config.example.json
    （由 tests/test_config_template.py 强制），所以"模板里没有"就是"上游不再认"。
    """
    removed: list[tuple[str, str]] = []

    def walk(tpl_node: object, cur_node: dict, prefix: str) -> None:
        tpl = tpl_node if isinstance(tpl_node, dict) else {}
        for key in list(cur_node.keys()):
            if key in SKIP_KEYS:
                continue
            path = f'{prefix}{key}'
            value = cur_node[key]

            if key not in tpl:
                if isinstance(value, dict):
                    # 模板里整段都没有：递归删它的叶子（逐个报，便于对照），
                    # 只有确实删掉了东西才把剩下的空壳摘掉 —— 空壳本身不单独记，
                    # 否则日志里会出现 `- server` 这种没信息量的行。
                    before = len(removed)
                    walk({}, value, f'{path}.')
                    if len(removed) > before and not value:
                        del cur_node[key]
                    continue
                removed.append((path, _safe_value(key, value)))
                del cur_node[key]
                continue

            # 模板里有这个键：两边都是对象才继续往下；否则保留用户的值
            if isinstance(value, dict) and isinstance(tpl[key], dict):
                walk(tpl[key], value, f'{path}.')

    walk(template, config, '')
    return removed


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
        print(f'[suite][WARN] 无法解析配置，跳过键集同步：{exc}', file=sys.stderr)
        return 0

    added = merge_missing(template, config)
    removed = prune_removed(template, config)
    if not added and not removed:
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

    print(f'[suite] 已同步 {config_path.name} 的键集（只增删键，不改任何值）：')
    for path in added:
        print(f'[suite]   + {path}   （上游新增，补齐）')
    for path, value in removed:
        print(f'[suite]   - {path} = {value}   （上游已移除，清理）')
    print(f'[suite] 原文件已备份为 {config_path.name}.bak')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
