"""`.dockerignore` 不得排除 Dockerfile 需要的任何东西。

为什么需要机械检查：`.dockerignore` 写错的失效方式很隐蔽 —— 构建期报
"file not found"，而报错指向的是 `COPY` 那一行，不会提到 `.dockerignore`。
文件头的注释反复强调"模式一律锚定"就是因为吃过这个亏：裸 `LICENSE` 会匹配到
任意层级，把 `vendor/manager/LICENSE`（Dockerfile 明确 COPY 的）一起排除。

本测试做两件事：
  1. 解析 Dockerfile 里**来自构建上下文**的 COPY 源（跳过 `--from=` 那些，
     它们取自构建阶段产物，与上下文无关），逐个断言没有被 `.dockerignore` 排除；
  2. 用几个已知正反例校验匹配器本身 —— 否则匹配器写错时，断言会在错误的
     基础上"通过"（上游测试里对这种空转有专门的防法，这里照做）。

匹配器只覆盖本项目实际用到的语法（锚定前缀、目录结尾斜杠、`*` / `**` / `?`）。
出现 `!` 取反这种没用到、又容易实现错的语法时，测试会直接报错要求扩展匹配器，
而不是猜。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO / 'docker' / 'Dockerfile'
_DOCKERIGNORE = _REPO / '.dockerignore'


def _read_patterns() -> list[str]:
    out: list[str] = []
    for raw in _DOCKERIGNORE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('!'):
            raise AssertionError(
                f'.dockerignore 里出现取反模式 {line!r} —— 本测试的匹配器没实现它。'
                '请扩展 tests/test_dockerignore.py 的 _matches()，别让它静默漏判。'
            )
        out.append(line)
    return out


def _glob_to_regex(pattern: str) -> str:
    """把 dockerignore 的通配转成正则片段。"""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '*':
            if pattern[i:i + 2] == '**':
                # `**/` 跨任意层级（含零层）
                if pattern[i:i + 3] == '**/':
                    out.append('(?:.*/)?')
                    i += 3
                    continue
                out.append('.*')
                i += 2
                continue
            out.append('[^/]*')
            i += 1
            continue
        if ch == '?':
            out.append('[^/]')
            i += 1
            continue
        if ch == '[':
            # Go 的 filepath.Match（dockerignore 用的语法）支持字符类，
            # 例如本文件里的 `**/*.py[cod]`。不处理它就会把 [cod] 当字面量，
            # 于是匹配器在真实模式上给出错误的"未排除"。
            end = pattern.find(']', i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            inner = pattern[i + 1:end]
            if inner.startswith('!'):
                inner = '^' + inner[1:]
            out.append('[' + inner + ']')
            i = end + 1
            continue
        out.append(re.escape(ch))
        i += 1
    return ''.join(out)


def _matches(pattern: str, path: str) -> bool:
    """pattern 是否排除 path（按 Docker 的层级语义）。"""
    anchored = pattern.startswith('/')
    body = pattern.lstrip('/').rstrip('/')
    regex = _glob_to_regex(body)
    # 模式命中目录时，其下内容也一并排除 —— 因此逐个前缀比对
    parts = path.split('/')
    candidates = ['/'.join(parts[: i + 1]) for i in range(len(parts))]
    for cand in candidates:
        if anchored:
            if re.fullmatch(regex, cand):
                return True
        else:
            if re.fullmatch(f'(?:.*/)?{regex}', cand):
                return True
    return False


def _excluded(path: str, patterns: list[str]) -> bool:
    return any(_matches(p, path) for p in patterns)


def _copy_sources() -> list[str]:
    """Dockerfile 里来自构建上下文的 COPY 源（跳过 --from=）。"""
    text = _DOCKERFILE.read_text(encoding='utf-8')
    # 续行拼接后再解析
    joined = re.sub(r'\\\s*\n', ' ', text)
    sources: list[str] = []
    for line in joined.splitlines():
        line = line.strip()
        if not line.upper().startswith('COPY '):
            continue
        tokens = line.split()[1:]
        if tokens and tokens[0].startswith('--from='):
            continue
        args = [t for t in tokens if not t.startswith('--')]
        if len(args) < 2:
            continue
        sources.extend(args[:-1])  # 末位是目标路径
    return sources


class MatcherSanityTest(unittest.TestCase):
    """先证明匹配器能识别那个真实的坑，否则下面的断言没有意义。"""

    def test_bare_pattern_hits_any_depth(self) -> None:
        """裸 `LICENSE` 会命中 vendor/manager/LICENSE —— 这就是必须锚定的原因。"""
        self.assertTrue(_matches('LICENSE', 'vendor/manager/LICENSE'))
        self.assertTrue(_matches('README.md', 'vendor/manager/README.md'))

    def test_anchored_pattern_spares_nested_paths(self) -> None:
        self.assertTrue(_matches('/LICENSE', 'LICENSE'))
        self.assertFalse(_matches('/LICENSE', 'vendor/manager/LICENSE'))

    def test_directory_pattern_covers_children(self) -> None:
        self.assertTrue(_matches('/tests', 'tests/test_x.py'))
        self.assertFalse(_matches('/tests', 'vendor/manager/tests/x.py'))

    def test_double_star(self) -> None:
        self.assertTrue(_matches('**/__pycache__', 'a/b/__pycache__'))
        self.assertTrue(_matches('**/__pycache__', 'a/b/__pycache__/x.pyc'))
        self.assertFalse(_matches('**/__pycache__', 'a/b/c.py'))

    def test_single_star_stays_within_segment(self) -> None:
        """`*` 不跨路径段（不像 `**`）。

        注意别拿"某个目录的子路径"当反例：命中了目录的模式会连带排除其下所有
        内容（Docker 的规则），那种情况下 True 才是对的。这里用的是确实不该
        命中的路径。
        """
        self.assertTrue(_matches('/a/*.txt', 'a/c.txt'))
        self.assertFalse(_matches('/a/*.txt', 'a/b/c.txt'))


class DockerignoreConsistencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.patterns = _read_patterns()
        self.sources = _copy_sources()

    def test_sources_were_actually_parsed(self) -> None:
        """先确认解析到了东西，否则下面的断言会在空集上通过。"""
        self.assertGreaterEqual(len(self.sources), 8, f'只解析到 {self.sources}')
        # Dockerfile 逐个拷贝 docker/ 下的文件（从不整目录 COPY docker/），
        # 所以这里检查"有来自这两个目录的源"，而不是目录本身出现过
        for prefix in ('vendor/', 'docker/'):
            with self.subTest(prefix=prefix):
                self.assertTrue(any(s.startswith(prefix) for s in self.sources),
                                f'Dockerfile 的 COPY 源里应有来自 {prefix} 的')
        self.assertIn('upstreams.json', self.sources)

    def test_no_copy_source_is_excluded(self) -> None:
        offenders = [s for s in self.sources if _excluded(s, self.patterns)]
        self.assertEqual(
            offenders, [],
            '这些 COPY 源被 .dockerignore 排除了 —— 构建期会报"file not found"，'
            f'而报错不会提到 .dockerignore：{offenders}',
        )

    def test_specific_files_the_dockerfile_needs_survive(self) -> None:
        """`vendor/` 是整目录 COPY，逐个点名它下面几个关键文件。

        上游 `.dockerignore` 同名文件的教训就是"整目录 COPY 里少了一个文件"，
        这里把它钉住：这些路径若被任何模式命中，说明有人加了过宽的规则。
        """
        needed = [
            'vendor/manager/LICENSE',                 # COPY 到 /opt/manager/LICENSE
            'vendor/manager/CHANGELOG.md',            # COPY 两处
            'vendor/manager/server/requirements.txt',  # pip install 用
            'vendor/manager/.github/workflows/release.yml',  # 上游测试要读
            'vendor/wb2api/go.sum',                   # go mod download 缓存键
            'vendor/wb2api/internal/prompt/defaultprompt.md',  # go:embed 必需
            'docker/patches/apply.py',
            'docker/overlay/suite_updater.py',
            'docker/wb2api.config.template.json',
            'upstreams.json',
        ]
        for path in needed:
            with self.subTest(path=path):
                self.assertTrue((_REPO / path).exists(), f'{path} 在仓库里不存在')
                self.assertFalse(_excluded(path, self.patterns),
                                 f'{path} 是构建输入，却被 .dockerignore 排除了')

    def test_secrets_and_data_stay_excluded(self) -> None:
        """另一头也要守住：数据目录与 .env 必须仍在排除之列。"""
        for path in ('data/config.json', 'data/auths/a.json', '.env', '.env.local',
                     'vendor/wb2api/config.json', 'vendor/wb2api/auths/x.json',
                     'vendor/manager/web/node_modules/react/index.js',
                     '.upstream-cache/wb2api.git/objects/x'):
            with self.subTest(path=path):
                self.assertTrue(_excluded(path, self.patterns),
                                f'{path} 必须被排除（机密或构建正确性）')

    def test_root_docs_are_excluded_but_not_nested_ones(self) -> None:
        """本次新增的根级裁剪：根文档排除，而 vendor 里的同名文件必须留下。"""
        for path in ('README.md', 'MAINTAINING.md', 'UPSTREAMS.md', 'CHANGELOG.md',
                     'LICENSE', 'tests/test_config_merge.py', 'scripts/sync-upstreams.sh',
                     '.github/workflows/build.yml'):
            with self.subTest(path=path):
                self.assertTrue(_excluded(path, self.patterns), f'{path} 应被排除（非构建输入）')
        self.assertFalse(_excluded('vendor/manager/README.md', self.patterns),
                         'vendor 里的同名文件不是我们的根文档，必须保留')
        self.assertFalse(_excluded('vendor/manager/LICENSE', self.patterns))
        self.assertFalse(_excluded('vendor/manager/.github/workflows/release.yml', self.patterns),
                         '根 /.github 的锚定不该波及 vendor 快照里的 .github')


if __name__ == '__main__':
    unittest.main()
