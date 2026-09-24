#!/usr/bin/env python3
"""报告有多少账号缺少 `device_token`（设备风控凭据）。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

===== 为什么需要它 =====
网关对外**声称自己是官方桌面客户端**（`X-Product: WorkBuddy`、`X-IDE-*`、
UA `WorkBuddy/5.5.4 … CLI/2.137.1`），而 `X-Device-Token` 只在账号带
`device_token` 时才发送（见 `vendor/wb2api/internal/upstream/headers.go` 的
`resolveDeviceToken` / `injectDeviceToken`：三级回退都取不到就**整条头不发送**）。

而**内置登录流程不写这个字段** —— `cmd/login`、`cmd/signin` 里没有任何写入
`device_token` 的代码。该字段只出现在**浏览器插件 OAuth 流程**产出的 auth 文件里，
或由使用者手工写入。因此用本项目流程加的账号，默认都会缺它。

缺了不报错、不影响功能 —— 这正是要在启动日志里说出来的原因：它会**静默降级
风控形态**，而使用者无从察觉。上游代码两处明说了这件事：

  * `vendor/manager/server/services/tencent.py`：「device_token 是设备风控凭据，
    丢了会静默降级风控形态」，并且它在换 token 重登时**专门保留**该字段；
  * 同文件 `_hdr()`：「缺失会被按设备指纹异常关联风控」。

===== 设计约束 =====
* **只读**：不修复、不写入。凭据只能由使用者在了解来源的前提下自己填，
  工具替它"补"一个假值反而更危险（假 token 比没有更糟）。
* **不打印凭据**，只报数量，避免把敏感值写进日志。
* **永不失败**：这是提示性检查，读不到目录、JSON 损坏一律当作"无法判断"，
  不能让容器起不来。
* 只在「有账号且有缺失」时输出，避免每次启动都刷屏。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def find_missing(auth_dir: Path) -> tuple[list[str], int]:
    """返回 (缺 device_token 的账号文件名, 账号总数)。

    只读顶层 `device_token`：auth 文件的扁平形与「插件 OAuth 嵌套形」都把该键
    放在**顶层**（见 internal/auth/auth.go：嵌套形把 device_token 声明在 auth 段
    之外，"手写时无需嵌进 auth 对象"）。
    """
    try:
        files = sorted(p for p in auth_dir.glob('*.json') if p.is_file())
    except Exception:  # noqa: BLE001
        return [], 0

    missing: list[str] = []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            # 解析不了的账号不参与判断（由别的检查负责报告坏文件）
            continue
        if not isinstance(doc, dict):
            continue
        if not str(doc.get('device_token') or '').strip():
            missing.append(path.stem)
    return missing, len(files)


def main(argv: list[str]) -> int:
    auth_dir = Path(argv[1]) if len(argv) > 1 else Path('/data/auths')
    missing, total = find_missing(auth_dir)
    if not missing or not total:
        return 0

    print(
        f'[suite][提示] {len(missing)}/{total} 个账号缺少 device_token（设备风控凭据）：'
        'X-Device-Token 不会发送，会被上游按设备指纹异常关联风控。',
        file=sys.stderr,
    )
    print(
        '[suite][提示] 补法与「查看哪些账号缺」的命令见 README「设备风控凭据」。',
        file=sys.stderr,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
