"""Apply reviewed wb2api patches before bundling the upstream Release snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess


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
            reverse = run('--reverse', '--check')
            if reverse.returncode == 0:
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
