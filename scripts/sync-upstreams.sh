#!/usr/bin/env bash
# ============================================================
# sync-upstreams.sh — 把 vendor/ 下的上游快照同步到目标分支的最新提交
#
# 用法：
#   ./scripts/sync-upstreams.sh                 # 同步全部上游
#   ./scripts/sync-upstreams.sh --only wb2api   # 只同步一个
#   ./scripts/sync-upstreams.sh --dry-run       # 只看差异，不落盘
#   ./scripts/sync-upstreams.sh --ref <commit>  # 固定到指定 commit（回退用）
#   ./scripts/sync-upstreams.sh --reexport      # commit 未变也重新导出快照
#                                               #（改了排除规则/导出逻辑后用）
#
# 它做什么：
#   1. 用本地裸仓库缓存 fetch 目标分支（不污染 vendor/）
#   2. 打印新旧 commit 之间的提交列表与 diff 统计
#   3. 把新版本导出到 vendor/<name>/（排除 .git、.github）
#   4. 回写 upstreams.json 的 commit / 日期 / subject
#
# 安全性：导出先在临时目录完成并校验，**通过后才替换** vendor/<name>/；
# 中途失败不会留下半个目录。vendor/ 是只读快照，本脚本不会改动上游代码。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MANIFEST="$ROOT/upstreams.json"
CACHE_DIR="$ROOT/.upstream-cache"

ONLY=""
DRY_RUN=0
FORCE_REF=""
REEXPORT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --only) ONLY="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --ref) FORCE_REF="${2:-}"; shift 2 ;;
        --reexport) REEXPORT=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数：$1（用 --help 查看用法）" >&2; exit 2 ;;
    esac
done

command -v git >/dev/null 2>&1 || { echo "需要 git" >&2; exit 1; }

# 探测可用的 Python 解释器。
# 为什么要探测而不是直接用 python3：Windows 上 `python3` 常是 Microsoft Store
# 的占位存根——命令存在、退出码 0 或 9009、但**什么都不执行也不输出**，
# 会让 JSON 解析静默返回空值。优先选能真正跑通 import json 的那个。
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import json,sys' >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
[ -n "$PY" ] || { echo "需要可用的 python3（或 python）" >&2; exit 1; }

[ -f "$MANIFEST" ] || { echo "未找到 $MANIFEST" >&2; exit 1; }

info() { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

# 读 upstreams.json 的字段：read_field <name> <field>
# 末尾 tr -d '\r'：Windows 上 Python 输出 CRLF，CR 会黏进后续的字符串比较
# （"wb2api\r" != "wb2api"），导致查不到字段。
read_field() {
    "$PY" - "$MANIFEST" "$1" "$2" <<'PY' | tr -d '\r'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    data = json.load(fh)
entry = (data.get('upstreams') or {}).get(sys.argv[2]) or {}
print(entry.get(sys.argv[3], ''))
PY
}

# 列出要处理的上游名
names() {
    "$PY" - "$MANIFEST" "$ONLY" <<'PY' | tr -d '\r'
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    data = json.load(fh)
keys = list((data.get('upstreams') or {}).keys())
only = sys.argv[2]
if only:
    if only not in keys:
        print(f'未知上游：{only}（可选：{", ".join(keys)}）', file=sys.stderr)
        sys.exit(2)
    keys = [only]
for k in keys:
    print(k)
PY
}

UPSTREAM_NAMES="$(names)" || exit $?

mkdir -p "$CACHE_DIR"

CHANGED=()

for NAME in $UPSTREAM_NAMES; do
    REPO_URL="$(read_field "$NAME" repo_url)"
    BRANCH="$(read_field "$NAME" branch)"
    OLD_COMMIT="$(read_field "$NAME" commit)"
    VENDOR_PATH="$(read_field "$NAME" vendor_path)"
    REPO_SLUG="$(read_field "$NAME" repo)"

    [ -n "$REPO_URL" ] || die "$NAME 缺少 repo_url"
    [ -n "$VENDOR_PATH" ] || die "$NAME 缺少 vendor_path"

    step "$NAME（$REPO_SLUG）"

    MIRROR="$CACHE_DIR/$NAME.git"
    # 注意 refs 布局：--bare 克隆把远端分支直接放在 refs/heads/*（没有
    # refs/remotes/origin/*）。所以 fetch 也用镜像式 refspec，两条路径
    # （首次克隆 / 后续 fetch）写入同一命名空间，避免 refs 落在两个地方。
    if [ -d "$MIRROR" ]; then
        info "更新本地镜像缓存 $MIRROR"
        git -C "$MIRROR" remote set-url origin "$REPO_URL"
        git -C "$MIRROR" fetch --prune --quiet origin '+refs/heads/*:refs/heads/*'
    else
        info "首次克隆镜像缓存（$REPO_URL）"
        git clone --bare --quiet "$REPO_URL" "$MIRROR"
    fi

    if [ -n "$FORCE_REF" ]; then
        # 指定 commit/ref：直接解析，允许回退到旧版本
        if ! NEW_COMMIT="$(git -C "$MIRROR" rev-parse --verify --quiet "${FORCE_REF}^{commit}")"; then
            # 镜像里没有该对象时尝试按 ref 获取
            git -C "$MIRROR" fetch --quiet origin "$FORCE_REF" || true
            NEW_COMMIT="$(git -C "$MIRROR" rev-parse --verify --quiet "${FORCE_REF}^{commit}")" \
                || die "无法解析 ref：$FORCE_REF"
        fi
        info "使用指定 ref：$FORCE_REF"
    else
        NEW_COMMIT="$(git -C "$MIRROR" rev-parse --verify --quiet "refs/heads/$BRANCH^{commit}")" \
            || die "镜像中找不到分支 $BRANCH（远端是否改名？请更新 upstreams.json 的 branch 字段）"
    fi

    NEW_SHORT="$(git -C "$MIRROR" rev-parse --short=7 "$NEW_COMMIT")"
    OLD_SHORT="${OLD_COMMIT:0:7}"

    META="$(git -C "$MIRROR" log -1 --format='%cI%n%s' "$NEW_COMMIT")"
    NEW_DATE="$(printf '%s' "$META" | sed -n '1p')"
    NEW_SUBJECT="$(printf '%s' "$META" | sed -n '2p')"

    if [ "$OLD_COMMIT" = "$NEW_COMMIT" ] && [ "$REEXPORT" != "1" ]; then
        ok "已是最新（$NEW_SHORT，$NEW_DATE）"
        continue
    fi

    if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
        # --reexport：commit 没变，但重新导出一次。
        # 用途：同步策略本身改动时（例如调整排除规则、修正导出逻辑），
        # 需要对同一个 commit 重新生成快照，否则旧快照会一直留着旧策略的产物。
        warn "commit 未变（$NEW_SHORT），按 --reexport 重新导出"
    fi

    if [ "$OLD_COMMIT" != "$NEW_COMMIT" ]; then
        echo "  当前锁定：$OLD_SHORT"
        echo "  远端最新：$NEW_SHORT  ($NEW_DATE)"
        echo "  提交说明：$NEW_SUBJECT"

        if git -C "$MIRROR" cat-file -e "$OLD_COMMIT^{commit}" 2>/dev/null; then
            echo ""
            echo "  ── 期间提交（旧 → 新）──"
            git -C "$MIRROR" log --oneline --no-decorate "$OLD_COMMIT..$NEW_COMMIT" | sed 's/^/    /'
            echo ""
            echo "  ── diff 统计 ──"
            git -C "$MIRROR" diff --stat "$OLD_COMMIT" "$NEW_COMMIT" | tail -20 | sed 's/^/    /'
        else
            warn "镜像中没有旧 commit $OLD_SHORT 的对象，跳过差异展示"
        fi
    fi

    if [ "$DRY_RUN" = "1" ]; then
        warn "dry-run：不落盘。确认无误后去掉 --dry-run 重跑"
        continue
    fi

    # ── 导出到临时目录，校验后整体替换 ──
    DEST="$ROOT/$VENDOR_PATH"
    STAGING="$(mktemp -d -t "suite-sync-$NAME-XXXXXX")"
    trap 'rm -rf "$STAGING"' EXIT

    info "导出 $NEW_SHORT 到临时目录"
    # 只排除 .git（体积大且无用）。**保留 .github**：
    #   * GitHub 只搜索仓库根目录的 .github/workflows，嵌套在 vendor/ 下的
    #     工作流不会被触发（已核实官方文档），因此留着无害；
    #   * 更重要的是 manager 的测试会读自己的 .github/workflows/release.yml
    #     （test_release_signature.py 断言发布流程上传 .sig 资产），
    #     排除掉会让上游测试直接报 FileNotFoundError —— 那是 vendoring 造成的
    #     假失败，会污染"测试全绿"这个信号。
    git -C "$MIRROR" archive "$NEW_COMMIT" \
        | tar -x -C "$STAGING" --exclude='.git'

    # 校验：防止把一个空/残缺的目录搬到 vendor/ 覆盖掉好快照
    if [ ! -f "$STAGING/LICENSE" ]; then
        die "导出结果缺少 LICENSE，已中止（vendor/ 未被改动）"
    fi
    FILE_COUNT="$(find "$STAGING" -type f | wc -l | tr -d ' ')"
    if [ "$FILE_COUNT" -lt 5 ]; then
        die "导出结果只有 $FILE_COUNT 个文件，疑似异常，已中止"
    fi

    # 洁净度校验：快照必须与上游 commit 逐字节一致。
    # 本地跑测试/构建会在 vendor/ 里落下这几类产物（manager 的版本自愈会写
    # .version，next build 会写 next-env.d.ts），若它们混进快照，
    # 「快照 == 上游某 commit」这个前提就破了，后续用 git archive 对比会对不上。
    # git archive 本身不会产出这些，这里是防御性的显式拦截。
    for stray in .version web/next-env.d.ts web/out web/.next web/node_modules; do
        if [ -e "$STAGING/$stray" ]; then
            die "导出结果含非上游产物 $stray，已中止（可能来自本地测试/构建）"
        fi
    done
    if find "$STAGING" -name '__pycache__' -type d | grep -q .; then
        die "导出结果含 __pycache__，已中止（可能来自本地测试）"
    fi

    # ── 必须移除上游的 .gitignore ─────────────────────────
    # 上游 .gitignore 的 bare 模式（`*.md`、`config.json`、`out/` 等）是**递归**的，
    # 而它现在嵌套在 vendor/<name>/ 下，于是会匹配到自己仓库里更深层的文件，
    # 把快照的必需文件悄悄排除掉。已发生过的事故：
    #
    #   vendor/wb2api/.gitignore 的 `*.md`  → 排除了
    #   internal/prompt/defaultprompt.md（go:embed 编译期必需文件）。
    #   本地 go build 正常（文件在磁盘上），CI 从 git 全新 clone 后文件不存在，
    #   直接 `pattern defaultprompt.md: no matching files found` 编译失败。
    #
    # 根 .gitignore 的否定规则**救不了**：Git 的合并规则是「深层 .gitignore 优先」，
    # 浅层的 `!path` 无法撤销深层 `*.md` 的排除。
    #
    # 因此统一移除，保护规则改由本项目根 .gitignore 用**锚定路径**表达
    # （见根 .gitignore 顶部的说明）。移除的是忽略规则本身，不是任何源文件。
    REMOVED_IGNORES="$(find "$STAGING" -name '.gitignore' | wc -l | tr -d ' ')"
    find "$STAGING" -name '.gitignore' -delete
    if [ "$REMOVED_IGNORES" -gt 0 ]; then
        info "移除 $REMOVED_IGNORES 个上游 .gitignore（其 bare 模式会误伤快照）"
    fi

    if [ -d "$DEST" ]; then
        info "替换 $VENDOR_PATH（原目录整体移除）"
        rm -rf "$DEST"
    fi
    mkdir -p "$(dirname "$DEST")"
    mv "$STAGING" "$DEST"
    trap - EXIT

    ok "$VENDOR_PATH 已更新为 $NEW_SHORT（$FILE_COUNT 个文件）"
    CHANGED+=("$NAME|$NEW_COMMIT|$NEW_DATE|$NEW_SUBJECT")
done

# ── 回写 upstreams.json ───────────────────────────────────
if [ "${#CHANGED[@]}" -gt 0 ] && [ "$DRY_RUN" != "1" ]; then
    step "更新 upstreams.json"
    for REC in "${CHANGED[@]}"; do
        IFS='|' read -r NAME COMMIT DATE SUBJECT <<< "$REC"
        "$PY" - "$MANIFEST" "$NAME" "$COMMIT" "$DATE" "$SUBJECT" <<'PY'
import json, sys
from datetime import datetime, timezone

manifest, name, commit, date, subject = sys.argv[1:6]
with open(manifest, encoding='utf-8') as fh:
    data = json.load(fh)
entry = data['upstreams'][name]
entry['commit'] = commit
entry['commit_short'] = commit[:7]
entry['commit_date'] = date
entry['subject'] = subject
entry['synced_at'] = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
with open(manifest, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write('\n')
print(f'  {name} -> {commit[:7]}')
PY
    done
    ok "upstreams.json 已更新"
fi

# ── 纳出完整性校验（防复发）───────────────────────────────
# 这一步堵的是本项目踩过的真实坑：快照文件在磁盘上存在，却被 .gitignore
# （尤其是嵌套的上游 .gitignore）悄悄排除，于是本地测试全绿、CI 从 git
# 全新 clone 后编译失败（defaultprompt.md 事件）。
#
# 判据：vendor/ 下**每一个磁盘文件**都必须出现在 git 索引里。
# 这是唯一的可靠信号——只看 `git add` 是否报错、或只看测试是否通过，都发现不了。
if [ "$DRY_RUN" != "1" ]; then
    step "校验 vendor 快照是否全部纳入 git"

    if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        warn "当前目录不是 git 仓库，跳过纳出校验"
        warn "  建议：git init 后跑一次本脚本，或手工执行"
        warn "    comm -23 <(find vendor -type f|sort) <(git ls-files vendor|sort)"
    else
        # 先把新快照登入索引，否则刚替换掉的文件在索引里还是旧状态，
        # 校验必然报"未纳入"（假阳性）。只看 vendor/，不动其他路径。
        git -C "$ROOT" add -A -- vendor >/dev/null 2>&1 || true

        DISK_LIST="$(mktemp)"; TRACKED_LIST="$(mktemp)"
        # 只用 NUL 分隔（-z / -print0）并配 sort -z / comm -z：
        # 避免两类路径表示差异造成的假阳性——
        #   1. 非 ASCII 文件名：git 默认 core.quotepath=true，会把中文路径
        #      转义成 "vendor/...\350\256\276..." 的八进制形式，与磁盘路径对不上；
        #   2. 含空格/特殊字符的路径被引号包裹。
        # -z 让 git 原样输出字节，不做任何转义或加引号。
        (
            cd "$ROOT"
            find vendor -type f \
                ! -path '*/auths/*' ! -path '*/data/*' \
                ! -name 'config.json' ! -name '.version' \
                ! -name 'next-env.d.ts' ! -path '*/node_modules/*' \
                ! -path '*/out/*' ! -path '*/.next/*' \
                ! -name '*.pyc' ! -path '*/__pycache__/*' \
                -print0 | sort -z
        ) > "$DISK_LIST"
        git -C "$ROOT" ls-files -z vendor | sort -z > "$TRACKED_LIST"

        MISSING="$(comm -z -23 "$DISK_LIST" "$TRACKED_LIST" | tr '\0' '\n')"
        if [ -n "$MISSING" ]; then
            echo "$MISSING" | sed 's/^/    /'
            warn "以上文件在 vendor/ 磁盘上存在，但**未被 git 纳入**。"
            warn "  CI 从 git 全新 clone 后会缺少这些文件，可能出现编译失败。"
            warn "  排查：git check-ignore -v <文件>  看是哪条 .gitignore 规则命中；"
            warn "  修法：把规则改成锚定路径（见根 .gitignore 顶部说明）。"
            rm -f "$DISK_LIST" "$TRACKED_LIST"
            die "纳出完整性校验未通过"
        fi
        # 计 NUL 个数而非行数：列表是 -z 分隔的，wc -l 对无换行的文件会误报
        TRACKED_COUNT="$(tr -dc '\0' < "$TRACKED_LIST" | wc -c | tr -d ' ')"
        rm -f "$DISK_LIST" "$TRACKED_LIST"
        ok "vendor 快照已全部纳入 git（$TRACKED_COUNT 个文件）"
    fi
fi

# ── 收尾提示 ─────────────────────────────────────────────
step "后续步骤"
if [ "$DRY_RUN" = "1" ]; then
    echo "  这是 dry-run，未做任何改动。"
else
    cat <<'EOF'
  1) 跑测试（三关）：
       (cd vendor/wb2api && go build ./... && go vet ./... && go test ./...)
       (cd vendor/manager && python3 -m unittest discover -s server/tests -t .)
       (cd vendor/manager/web && npm ci && npm run build:export)

  2) 检查是否需要同步集成层（见 UPSTREAMS.md 的检查清单）：
       - 上游改了 Dockerfile 依赖  → docker/Dockerfile
       - 上游改了 config.example   → docker/wb2api.config.template.json
       - 上游改了 /api/system 约定 → docker/stub/update.py

  3) 同步 UPSTREAMS.md 的表格与套件 CHANGELOG.md，然后提交。
EOF
fi
