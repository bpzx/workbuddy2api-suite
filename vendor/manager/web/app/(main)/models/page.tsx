'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Boxes,
  Brain,
  Gauge,
  Layers,
  Loader2,
  Maximize2,
  RefreshCw,
  Search,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {modelApi, errText} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {notify} from '@/lib/toast';
import {cn} from '@/lib/utils';
import type {CatalogModel, ModelCatalog} from '@/lib/types';

/** 上下文窗口显示：131072 → 128K；1048576 → 1M；0 → — */
function fmtCtx(n: number): string {
  if (!n || n <= 0) return '—';
  if (n >= 1024 * 1024) {
    const m = n / (1024 * 1024);
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
}

/** 系列标签配色（按系列名稳定取色，认不出的用中性色） */
const SERIES_STYLE: Record<string, string> = {
  '智谱 GLM': 'border-sky-500/30 bg-sky-500/10 text-sky-700 dark:text-sky-400',
  DeepSeek: 'border-indigo-500/30 bg-indigo-500/10 text-indigo-700 dark:text-indigo-400',
  Kimi: 'border-violet-500/30 bg-violet-500/10 text-violet-700 dark:text-violet-400',
  MiniMax: 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-400',
  腾讯混元: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-700 dark:text-cyan-400',
  自动选择: 'border-border bg-muted text-muted-foreground',
};

function SeriesBadge({series}: {series: string}) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium',
        SERIES_STYLE[series] ?? 'border-border bg-muted text-muted-foreground',
      )}
    >
      {series}
    </span>
  );
}

/** 一张统计卡 */
function StatCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Boxes;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-[20px] bg-muted px-3.5 py-3">
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-1.5 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground/80">{hint}</div>}
    </div>
  );
}

export default function ModelsPage() {
  const {realm, label: realmName} = useRealm();
  const [data, setData] = useState<ModelCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const [q, setQ] = useState('');
  const [series, setSeries] = useState('all');
  /** 能力筛选：全部 / 支持推理 / 大上下文 / 多模态 */
  const [cap, setCap] = useState<'all' | 'reasoning' | 'large' | 'vision'>('all');

  // realm 变化时重新拉取：两个版本的模型清单不同，且后端已按版本分开缓存
  const load = useCallback(async (force = false) => {
    if (force) setRefreshing(true);
    else setLoading(true);
    try {
      const res = await modelApi.catalog(realm, force);
      setData(res);
      setError('');
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [realm]);

  useEffect(() => {
    // 切版本时清掉筛选状态，避免「上一版的系列筛选把新版过滤成空」
    setSeries('all');
    setCap('all');
    setQ('');
    load();
  }, [load]);

  const models = data?.models ?? [];

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return models.filter((m) => {
      if (series !== 'all' && m.series !== series) return false;
      if (cap === 'reasoning' && m.efforts.length === 0) return false;
      if (cap === 'large' && (m.context_length || 0) < 131072) return false;
      if (cap === 'vision' && !m.supports_images) return false;
      if (!kw) return true;
      return (
        m.id.toLowerCase().includes(kw) ||
        (m.name || '').toLowerCase().includes(kw) ||
        m.series.toLowerCase().includes(kw)
      );
    });
  }, [models, q, series, cap]);

  const summary = data?.summary;
  const seriesOptions = summary?.series ?? [];

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="模型中心"
        description={`${realmName}账号可用的模型及上下文、输出与推理能力（数据取自腾讯模型接口）`}
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            disabled={refreshing}
            title="重新拉取（正常按 5 分钟缓存，避免频繁请求触发风控）"
            onClick={() => {
              load(true);
              notify.info('正在重新拉取模型列表…');
            }}
          >
            {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            重新拉取
          </Button>
        }
      />

      {/* 来源说明：如实标注，不把回退数据说成实时数据 */}
      {data && !loading && (
        <div
          className={cn(
            'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border px-3.5 py-2.5 text-[11px]',
            data.source === 'tencent'
              ? 'border-border bg-muted/60 text-muted-foreground'
              : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
          )}
        >
          <span className="font-medium">数据来源：{data.source_label}</span>
          {data.via && <span>· 取自 {data.via}</span>}
          {data.cached && <span>· 缓存 {data.cache_age} 秒前</span>}
          {data.source !== 'tencent' && (
            <span className="basis-full text-[10px] leading-4 opacity-90">
              {data.source === 'upstream'
                ? '腾讯模型接口未取到（通常是账号凭证过期或网络问题），当前展示的是上游返回的简表，缺少显示名与推理档位。可在「账号」页刷新令牌后重新拉取。'
                : '腾讯模型接口与上游均未取到模型，请确认账号可用且上游容器在运行。'}
            </span>
          )}
        </div>
      )}

      {/* 统计卡 */}
      {summary && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard icon={Boxes} label="可用模型" value={String(summary.total)} />
          <StatCard
            icon={Brain}
            label="支持推理"
            value={String(summary.reasoning)}
            hint="带推理档位（low/high/max）"
          />
          <StatCard
            icon={Maximize2}
            label="大上下文"
            value={String(summary.large_context)}
            hint="≥128K"
          />
          <StatCard
            icon={Gauge}
            label="最大上下文"
            value={fmtCtx(summary.max_context)}
            hint={summary.series.length ? `${summary.series.length} 个系列` : undefined}
          />
        </div>
      )}

      {/* 搜索与筛选 */}
      <section className="rounded-[20px] bg-muted p-3.5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索模型名 / id / 系列"
              className="h-9 bg-background pl-8"
            />
          </div>
          <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
            <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
              <Layers className="h-3.5 w-3.5" />
              系列
            </span>
            <Button
              variant={series === 'all' ? 'default' : 'outline'}
              size="sm"
              className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
              onClick={() => setSeries('all')}
            >
              全部
            </Button>
            {seriesOptions.map((s) => (
              <Button
                key={s}
                variant={series === s ? 'default' : 'outline'}
                size="sm"
                className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                onClick={() => setSeries(s)}
              >
                {s}
              </Button>
            ))}
            <span className="ml-1 h-4 w-px shrink-0 bg-border" />
            {(
              [
                ['all', '全部'],
                ['reasoning', '支持推理'],
                ['large', '大上下文'],
                ['vision', '多模态'],
              ] as const
            ).map(([k, label]) => (
              <Button
                key={k}
                variant={cap === k ? 'default' : 'outline'}
                size="sm"
                className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                onClick={() => setCap(k)}
              >
                {label}
              </Button>
            ))}
          </div>
        </div>
      </section>

      {/* 列表 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在读取模型列表…
          </div>
        ) : error ? (
          <EmptyState icon={Boxes} title="读取失败" description={error} />
        ) : models.length === 0 ? (
          <EmptyState
            icon={Boxes}
            title="暂无模型"
            description="未从腾讯模型接口或上游取到任何模型，请确认账号可用且上游容器在运行。"
          />
        ) : filtered.length === 0 ? (
          <EmptyState icon={Search} title="没有匹配的模型" description="换个关键词或清除筛选条件试试。" />
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">模型</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">上下文</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">最大输出</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">推理档位</TableHead>
                    <TableHead className="pr-4 text-[11px] text-muted-foreground">系列</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((m: CatalogModel) => (
                    <TableRow key={m.id} className="border-b border-border/40">
                      <TableCell className="pl-4">
                        <div className="flex flex-col gap-0.5 py-0.5">
                          {m.name ? (
                            <>
                              <span className="text-xs font-medium">{m.name}</span>
                              <span className="font-mono text-[10px] text-muted-foreground">{m.id}</span>
                            </>
                          ) : (
                            <span className="font-mono text-xs font-medium">{m.id}</span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.context_length)}
                      </TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.max_output_tokens)}
                      </TableCell>
                      <TableCell>
                        {m.efforts.length ? (
                          <div className="flex flex-wrap items-center gap-1">
                            {m.efforts.map((e) => (
                              <Badge key={e} variant="secondary" className="rounded-md font-mono text-[10px]">
                                {e}
                              </Badge>
                            ))}
                            {/* 默认档位单独标出来：上游 thinking 决策用它，
                                用户据此知道不指定档位时会走哪一档 */}
                            {m.default_effort && (
                              <span
                                className="text-[10px] text-muted-foreground"
                                title={`未指定档位时默认使用 ${m.default_effort}`}
                              >
                                默认 {m.default_effort}
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="text-[11px] text-muted-foreground/60">—</span>
                        )}
                      </TableCell>
                      <TableCell className="pr-4">
                        <div className="flex items-center justify-end gap-1.5">
                          {m.supports_images && (
                            <Badge variant="secondary" className="rounded-md text-[10px]" title="支持图片输入">
                              多模态
                            </Badge>
                          )}
                          <SeriesBadge series={m.series} />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {(filtered.length !== models.length || q.trim() || series !== 'all' || cap !== 'all') && (
              <div className="border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
                已筛选出 {filtered.length} 个（共 {models.length} 个）
              </div>
            )}
          </>
        )}
      </section>

      {/* 失败原因：默认收起，供排查（例如某个账号凭证过期） */}
      {!!data?.errors?.length && (
        <details className="rounded-[16px] bg-muted/60 px-3.5 py-2.5 text-[11px] text-muted-foreground">
          <summary className="cursor-pointer select-none">拉取过程中的失败记录（{data.errors.length}）</summary>
          <ul className="mt-2 space-y-1 font-mono text-[10px] leading-4">
            {data.errors.map((e, i) => (
              <li key={i} className="break-all">· {e}</li>
            ))}
          </ul>
        </details>
      )}

      <p className="text-[10px] leading-4 text-muted-foreground/70">
        说明：上下文与输出上限来自腾讯模型接口；推理档位为该模型支持的 reasoning effort。
        「系列」按模型 id 前缀推导，仅用于分组浏览。倍数/计费单价腾讯未提供，故不在此展示。
      </p>
    </div>
  );
}
