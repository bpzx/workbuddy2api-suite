'use client';

import {useEffect, useState} from 'react';
import {ChevronRight, FileText, Loader2, Package, TriangleAlert} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {systemApi} from '@/lib/api';
import {cn} from '@/lib/utils';
import type {Changelog, ChangelogItem} from '@/lib/types';

const REPO_URL = 'https://github.com/ithtelab/workbuddy-manager/releases';

/** 分类配色，让「安全」「修复」这类一眼可辨 */
const SECTION_STYLE: Record<string, string> = {
  安全: 'border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400',
  新增: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  修复: 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400',
  改进: 'border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400',
  说明: 'border-border bg-muted text-muted-foreground',
  计划中: 'border-violet-500/30 bg-violet-500/10 text-violet-600 dark:text-violet-400',
};

/**
 * 轻量行内渲染：只处理 `**加粗**` 与 `` `代码` ``，不引入 markdown 依赖。
 * 更新日志格式固定，用不着完整解析器——少一个依赖，离线包也更小。
 */
function InlineText({text}: {text: string}) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**')) {
          return (
            <strong key={i} className="font-medium text-foreground">
              {part.slice(2, -2)}
            </strong>
          );
        }
        if (part.startsWith('`') && part.endsWith('`')) {
          return (
            <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">
              {part.slice(1, -1)}
            </code>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function ItemList({items}: {items: ChangelogItem[]}) {
  const hasSub = items.some((it) => it.level > 0);
  return (
    <ul className={cn('space-y-1.5', hasSub && 'space-y-2')}>
      {items.map((it, i) => (
        <li
          key={i}
          className={cn(
            'flex gap-2 text-xs leading-5 text-muted-foreground',
            it.level > 0 && 'ml-3.5 border-l border-border pl-2.5',
          )}
        >
          <span
            className={cn(
              'mt-[7px] h-1 w-1 shrink-0 rounded-full',
              it.level > 0 ? 'bg-muted-foreground/40' : 'bg-muted-foreground/70',
            )}
          />
          <span className="min-w-0 flex-1 break-words">
            <InlineText text={it.text} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function VersionBlock({
  index,
  version,
  date,
  unreleased,
  isCurrent,
  currentVersion,
  expanded,
  onToggle,
  sections,
}: {
  index: number;
  version: string;
  date: string;
  unreleased: boolean;
  isCurrent: boolean;
  currentVersion: string;
  expanded: boolean;
  onToggle: () => void;
  sections: Changelog['versions'][number]['sections'];
}) {
  const count = sections.reduce((n, s) => n + s.items.length, 0);
  // 当前版本置顶时也默认展开，方便一眼看到最新改了什么
  return (
    <div
      className={cn(
        'overflow-hidden rounded-[16px] border transition-colors',
        expanded ? 'border-border bg-background' : 'border-transparent bg-muted/50',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left transition-colors hover:bg-muted/60"
      >
        <ChevronRight
          className={cn(
            'h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform',
            expanded && 'rotate-90',
          )}
        />
        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-mono text-xs font-medium">
            {unreleased ? '未发布' : `v${version}`}
          </span>
          {isCurrent && !unreleased && (
            <Badge variant="secondary" className="h-5 px-1.5 text-[10px]">
              当前版本
            </Badge>
          )}
          {unreleased && (
            <Badge
              variant="outline"
              className="h-5 border-amber-500/40 bg-amber-500/10 px-1.5 text-[10px] text-amber-600 dark:text-amber-400"
            >
              开发中
            </Badge>
          )}
          {date && <span className="text-[11px] text-muted-foreground">{date}</span>}
        </span>
        {!expanded && (
          <span className="shrink-0 text-[11px] text-muted-foreground">{count} 项</span>
        )}
      </button>

      {expanded && (
        <div className="space-y-3.5 border-t border-border px-3.5 py-3.5">
          {sections.map((sec, si) => (
            <div key={si} className="space-y-2">
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'rounded-md border px-1.5 py-0.5 text-[10px] font-medium',
                    SECTION_STYLE[sec.title] ?? 'border-border bg-muted text-muted-foreground',
                  )}
                >
                  {sec.title}
                </span>
                <span className="text-[10px] text-muted-foreground/70">
                  {sec.items.length} 项
                </span>
              </div>
              <ItemList items={sec.items} />
            </div>
          ))}
          {isCurrent && currentVersion && !unreleased && (
            <div className="pt-0.5 text-[10px] text-muted-foreground/70">
              你当前运行的正是这个版本
            </div>
          )}
          {index === 0 && !unreleased && (
            <div className="pt-0.5 text-[10px] text-muted-foreground/70">
              查看全部版本：见仓库 Releases 页面
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ChangelogPanel() {
  const [data, setData] = useState<Changelog | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    systemApi
      .changelog()
      .then((d) => {
        if (!alive) return;
        setData(d);
        if (!d.available) setError(d.error || '更新日志不可用');
        else if (d.versions.length) setOpen(d.versions[0].version);
      })
      .catch(() => alive && setError('读取更新日志失败'))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-[20px] bg-muted py-16 text-xs text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        正在读取更新日志…
      </div>
    );
  }

  if (error || !data?.available) {
    return (
      <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
        <div className="space-y-1 text-xs">
          <div className="font-medium">更新日志不可用</div>
          <div className="text-muted-foreground">{error || data?.error}</div>
          <div className="text-muted-foreground">
            📄 更新日志随发布包一起分发（<code className="font-mono">CHANGELOG.md</code>）。
            若你是从旧版本升级上来的，去「系统更新」执行一次更新即可补上；
            从源码运行时，请确认仓库根目录存在该文件。
          </div>
        </div>
      </div>
    );
  }

  const currentVersion = (data.current || '').replace(/^v/i, '');

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <FileText className="h-4 w-4" />
          更新日志
          {currentVersion && (
            <span className="font-mono text-xs text-muted-foreground">
              当前 v{currentVersion}
            </span>
          )}
        </div>
        <Button asChild variant="ghost" size="sm" className="h-7 gap-1.5 text-xs">
          <a href={REPO_URL} target="_blank" rel="noreferrer">
            <Package className="h-3.5 w-3.5" />
            Releases
          </a>
        </Button>
      </div>

      <div className="scroll-slim max-h-[calc(100dvh-260px)] min-h-[320px] space-y-2 overflow-y-auto pr-1">
        {data.versions.map((v, i) => (
          <VersionBlock
            key={v.version}
            index={i}
            version={v.version}
            date={v.date}
            unreleased={v.unreleased}
            isCurrent={!v.unreleased && v.version.replace(/^v/i, '') === currentVersion}
            currentVersion={currentVersion}
            expanded={open === v.version}
            onToggle={() => setOpen(open === v.version ? null : v.version)}
            sections={v.sections}
          />
        ))}
      </div>

      {data.truncated && (
        <p className="text-[11px] text-muted-foreground">
          仅显示最近 {data.versions.length} 个版本（共 {data.total} 个），更早的请查看仓库 Releases 页面。
        </p>
      )}
    </div>
  );
}
