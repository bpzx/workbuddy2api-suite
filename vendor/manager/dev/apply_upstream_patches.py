"""Apply reviewed wb2api patches before bundling the upstream Release snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess

_HUNK = re.compile(r'^@@')
_FILE = re.compile(r'^\+\+\+ b/(.+)$')


def _added_lines(patch: Path) -> dict[str, list[str]]:
    """解析补丁，返回 {目标文件: [新增行内容]}（不含 `+` 前缀）。"""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for raw in patch.read_text(encoding='utf-8').splitlines():
        m = _FILE.match(raw)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if _HUNK.match(raw):
            continue
        if current is None or not raw.startswith('+') or raw.startswith('+++'):
            continue
        out[current].append(raw[1:])
    return out


def _already_applied(patch: Path, source: Path) -> bool:
    """补丁的新增行是否**都已在源码里**（= 这个补丁已经打过了）。

    为什么不沿用 `git apply --reverse --check`：后面新增的补丁一旦动到前面补丁的
    上下文邻域（哪怕只是同一小段里的另一处改动），反向校验就会失败，于是整条流水线
    在「重入」这一步报「patch does not match this snapshot」—— 而内容其实完全正确。
    本仓的补丁是**顺序应用于同一份资产**的切片，这种上下文漂移是常态。

    判据只看新增行（上下文行不参与）：对「已经应用」这个判断足够，且不受其它补丁
    漂移影响。空行不判（到处都是）。新增行全为空的新文件补丁按「未应用」处理 ——
    那种情况下真正该走的是正常 apply。
    """
    files = _added_lines(patch)
    if not files:
        return False
    checked = 0
    for rel, lines in files.items():
        target = source / rel
        if not target.is_file():
            return False
        text = target.read_text(encoding='utf-8', errors='replace')
        for line in lines:
            if not line.strip():
                continue
            checked += 1
            if line not in text:
                return False
    return checked > 0


def apply_patches(source: Path, patch_dir: Path) -> list[str]:
    source = source.resolve()
    patch_dir = patch_dir.resolve()
    if not (source / 'go.mod').is_file() or not (source / 'docker-compose.yml').is_file():
        raise ValueError(f'Not a workbuddy2api source root: {source}')
    patches = sorted(patch_dir.glob('*.patch'))
    if not patches:
        raise ValueError(f'No upstream patches found in {patch_dir}')
    results = []
    env = os.environ.copy()
    # Release staging lives inside the manager checkout. Do not let git discover
    # that parent repository and silently skip paths belonging to the upstream.
    env['GIT_CEILING_DIRECTORIES'] = str(source.parent)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(key, None)
    for patch in patches:
        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(['git', 'apply', *args, str(patch)], cwd=source,
                                  env=env, capture_output=True, text=True)
        check = run('--check')
        if check.returncode:
            # 内容判定优先：只要新增行都在，就是「已应用」（见 _already_applied 的说明）。
            if _already_applied(patch, source) or run('--reverse', '--check').returncode == 0:
                results.append(f'already applied: {patch.name}')
                continue
            raise RuntimeError(f'Upstream patch does not match this snapshot: {patch.name}\n'
                               f'{check.stderr.strip()}\nReview/rebase the patch before publishing.')
        applied = run()
        if applied.returncode:
            raise RuntimeError(f'Could not apply {patch.name}: {applied.stderr.strip()}')
        results.append(f'applied: {patch.name}')
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--patch-dir', type=Path,
                        default=Path(__file__).resolve().parent / 'upstream-patches')
    args = parser.parse_args()
    try:
        for result in apply_patches(args.source, args.patch_dir):
            print(result)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
