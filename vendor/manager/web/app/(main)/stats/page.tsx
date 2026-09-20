'use client';

import {useCallback, useEffect, useState} from 'react';
import {Activity, TrendingUp, KeyRound, Cpu, Wrench, RotateCcw, Coins, AlertTriangle} from 'lucide-react';
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {statsApi, errText} from '@/lib/api';
import type {StatsSummary, UsageBreakdown, UsagePoint} from '@/lib/types';
import {fmtCompact, fmtNumber, fmtCredit} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';

const CHART_COLORS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
];

/**
 * 统计健康提示的详情本地化。
 *
 * 服务端（`server/routers/stats.py` 的 `_usage_health`）同时返回**结构化数字**
 * `logs_today` 和一句中文 `detail`。这里用数字拼译文，**不再正则反解那句中文**。
 *
 * 为什么改掉正则：原先按 `^今天已有 (\d+) 次…` 反解，后来服务端为了标明版本
 * 把文案改成 `今天国内版已有 N 次…`，正则立刻全都不匹配 —— 结果是五种语言的
 * 译文全部失效，非中文用户看到原始中文句子，而且**不会有任何报错**。
 * 用结构化字段就没有这种「改文案即静默失效」的耦合。
 *
 * 退路：拿不到数字（服务端旧版本）时原样显示 `detail`，不显示空白。
 */
function usageHealthDetail(
  health: {detail?: string; logs_today?: number},
  t: (key: string, params?: Record<string, string>) => string,
): string {
  const n = health.logs_today;
  if (typeof n === 'number' && n > 0) {
    return t('stats.usageHealthDetail', {n: fmtNumber(n)});
  }
  return health.detail || '';
}

export default function StatsPage() {
  const {isAdmin} = useAuth();
  // 统计随顶部版本切换：两个版本走的是不同账号池，混在一起看没有意义
  const {realm, label: realmName} = useRealm();
  const t = useT();
  const [summary, setSummary] = useState<StatsSummary | null>(null);
  const [daily, setDaily] = useState<UsagePoint[]>([]);
  const [byModel, setByModel] = useState<UsageBreakdown[]>([]);
  const [byKey, setByKey] = useState<UsageBreakdown[]>([]);
  const [days, setDays] = useState('30');
  const load = useCallback(async () => {
    const d = Number(days) || 30;
    const results = await Promise.allSettled([
      statsApi.summary(realm),
      statsApi.daily(d, realm),
      statsApi.byModel(d, realm),
      statsApi.byKey(d, realm),
    ]);
    if (results[0].status === 'fulfilled') setSummary(results[0].value);
    if (results[1].status === 'fulfilled') setDaily(results[1].value);
    if (results[2].status === 'fulfilled') setByModel(results[2].value);
    if (results[3].status === 'fulfilled') setByKey(results[3].value);
    if (results.some((r) => r.status === 'rejected')) notify.err(errText((results.find((r) => r.status === 'rejected') as PromiseRejectedResult).reason));
  }, [days, realm, t]);

  useEffect(() => {
    load();
  }, [load]);

  // 用量随调用持续累计，心跳刷新让页面保持接近实时
  useHeartbeat(load, 60000);

  const chartData = daily.map((d) => ({
    day: d.day.slice(5),
    tokens: d.prompt_tokens + d.completion_tokens,
    requests: d.requests,
    // 失败曲线来自另一份数据源（请求日志），与用量汇总不构成堆叠关系
    failed: d.failed ?? 0,
  }));

  /** 今日失败总数（4xx + 5xx）。单独来自请求日志——用量汇总只含成功请求。 */
  const todayFailed =
    (summary?.failures?.today_4xx ?? 0) + (summary?.failures?.today_5xx ?? 0);

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('stats.title')}
        description={t('stats.description', {realm: realmName})}
        actions={
          <>
            <Select value={days} onValueChange={setDays}>
              <SelectTrigger className="h-8 w-[130px] rounded-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="7">{t('stats.last7')}</SelectItem>
                <SelectItem value="30">{t('stats.last30')}</SelectItem>
                <SelectItem value="90">{t('stats.last90')}</SelectItem>
              </SelectContent>
            </Select>
            {isAdmin && (
              <ConfirmDialog
                title={t('stats.repairTitle')}
                description={t('stats.repairDesc')}
                confirmText={t('stats.repairStart')}
                onConfirm={async () => {
                  try {
                    const r = await statsApi.repairUsage();
                    if (r.repaired > 0) {
                      notify.ok(
                        t('stats.repaired'),
                        t('stats.repairedDetail', {
                          requests: fmtNumber(r.requests),
                          tokens: fmtNumber(r.tokens),
                        }),
                      );
                    } else {
                      notify.info(t('stats.nothingToRepair'), t('stats.nothingToRepairDesc'));
                    }
                    await load();
                  } catch (e) {
                    notify.err(errText(e));
                  }
                }}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full">
                    <Wrench className="h-3.5 w-3.5" />
                    {t('stats.repairButton')}
                  </Button>
                }
              />
            )}
            {isAdmin && (
              <ConfirmDialog
                title={t('stats.rebuildTitle')}
                description={t('stats.rebuildDesc')}
                confirmText={t('stats.rebuildStart')}
                destructive
                onConfirm={async () => {
                  try {
                    const r = await statsApi.rebuildUsage();
                    const d = r.tokens_delta;
                    notify.ok(
                      t('stats.rebuilt'),
                      t('stats.rebuiltDetail', {
                        before: fmtNumber(r.rows_before),
                        after: fmtNumber(r.rows_after),
                      }) +
                        (d !== 0
                          ? t('stats.rebuiltDetailTokens', {
                              delta: `${d > 0 ? '+' : ''}${fmtNumber(d)}`,
                            })
                          : t('stats.rebuiltDetailSame')),
                    );
                    await load();
                  } catch (e) {
                    notify.err(errText(e));
                  }
                }}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full text-amber-600 dark:text-amber-400">
                    <RotateCcw className="h-3.5 w-3.5" />
                    {t('stats.rebuildButton')}
                  </Button>
                }
              />
            )}
          </>
        }
      />

      {/* 统计没在累计时明确提示。
          统计是旁路写入（失败不影响转发），所以坏了以后界面看不出异常——
          数字只是停着不动、页面照常刷新。这里把「今天有请求但统计为 0」
          这种组合直接摆出来，并指向右侧的「修复统计」按钮。 */}
      {summary?.usage_health && !summary.usage_health.ok && (
        <div className="flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <div className="text-[11px] leading-5">
            <span className="font-medium text-amber-700 dark:text-amber-300">
              {t('stats.usageHealthTitle')}
            </span>
            <div className="text-muted-foreground">
              {usageHealthDetail(summary.usage_health, t)}
            </div>
          </div>
        </div>
      )}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 md:gap-4">
        <StatCard
          label={t('stats.todayRequests')}
          value={fmtNumber(summary?.today_requests)}
          hint={
            // 有失败时把失败数说在前面：这个卡片是用户查「今天出了什么事」的
            // 第一站，而「用量 N token」是次要信息。
            //
            // 也要说清**失败不计入上面的数字**：请求数来自用量汇总（只含成功
            // 请求），所以「今天 0 次请求 + 31 次失败」是可能的——那正是全池
            // 中断的形态。不说清的话用户会以为卡片坏了。
            todayFailed > 0
              ? t('stats.failedHint', {n: fmtNumber(todayFailed)})
              : t('stats.tokenHint', {v: fmtCompact(summary?.today_tokens)})
          }
          icon={Activity}
          tone={todayFailed > 0 ? 'warning' : 'info'}
          hintTone={todayFailed > 0 ? 'warning' : undefined}
          delay={0}
        />
        <StatCard
          label={t('stats.weekRequests')}
          value={fmtNumber(summary?.week_requests)}
          hint={t('stats.tokenHint', {v: fmtCompact(summary?.week_tokens)})}
          icon={TrendingUp}
          tone="accent"
          delay={0.05}
        />
        <StatCard
          label={t('stats.todayPaid')}
          value={fmtCredit(summary?.today_credit)}
          hint={
            summary?.today_credit
              ? t('stats.todayPaidHint', {v: fmtCredit(summary?.week_credit)})
              : t('stats.noCreditFromUpstream')
          }
          icon={Coins}
          tone="warning"
          delay={0.1}
        />
        <StatCard
          label={t('stats.activeKeys')}
          value={fmtNumber(summary?.active_keys)}
          hint={summary?.top_model ? t('stats.topModel', {model: summary.top_model}) : t('stats.distributing')}
          icon={KeyRound}
          tone="success"
          delay={0.15}
        />
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm font-medium">{t('stats.tokenTrend')}</div>
          <div className="text-[11px] text-muted-foreground">{t('stats.dailyAgg')}</div>
        </div>
        <div className="h-[260px] w-full">
          {chartData.length ? (
            <ResponsiveContainer width="100%" height="100%">
              {/* 用 ComposedChart 而不是 BarChart：BarChart 会忽略非 Bar 子组件，
                  失败曲线（Line）根本不会被渲染 —— 实测踩过，图上一条线都没有。 */}
              <ComposedChart data={chartData} margin={{top: 4, right: 8, bottom: 0, left: -8}} barCategoryGap="20%">
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" />
                <YAxis tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" tickFormatter={(v) => fmtCompact(Number(v))} />
                <Tooltip
                  cursor={{fill: 'var(--accent)'}}
                  contentStyle={{
                    background: 'var(--popover)',
                    border: '1px solid var(--border)',
                    borderRadius: 12,
                    fontSize: 12,
                  }}
                  formatter={(value, name) => {
                    const label = String(name);
                    if (label === 'tokens') return [fmtNumber(Number(value)), 'Token'];
                    if (label === 'failed') {
                      return [fmtNumber(Number(value)), t('dashboard.failedRequests')];
                    }
                    return [fmtNumber(Number(value)), t('metric.requests')];
                  }}
                />
                <Bar
                  dataKey="tokens"
                  name="tokens"
                  fill="var(--chart-1)"
                  radius={[4, 4, 0, 0]}
                  /* 限制柱宽：只有一两天数据时，柱子不会被拉伸占满整个图表 */
                  maxBarSize={48}
                />
                {/* 失败数用一条线叠在同一张图上：它与 token 柱不同量级，做成柱子
                    会把柱形压扁。线只作「那天出过事」的信号，具体数值看悬停。
                    没有失败时贴着 0，不干扰读数。 */}
                <Line
                  type="monotone"
                  dataKey="failed"
                  name="failed"
                  stroke="var(--destructive)"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  dot={false}
                />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <div className="grid h-full place-items-center text-xs text-muted-foreground">{t('stats.noUsageData')}</div>
          )}
        </div>
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <BreakdownPanel title={t('stats.byModel')} icon={Cpu} items={byModel} />
        <BreakdownPanel title={t('stats.byKey')} icon={KeyRound} items={byKey} />
      </section>
    </div>
  );
}

function BreakdownPanel({
  title,
  icon: Icon,
  items,
}: {
  title: string;
  icon: typeof Cpu;
  items: UsageBreakdown[];
}) {
  const t = useT();
  const max = Math.max(1, ...items.map((i) => i.prompt_tokens + i.completion_tokens));
  return (
    <div className="rounded-[20px] bg-muted p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Icon className="h-4 w-4" />
        {title}
      </div>
      {items.length ? (
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-0 text-[11px] text-muted-foreground">{t('metric.name')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('metric.requestsShort')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              <TableHead className="pr-0 text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((it, i) => {
              const tokens = it.prompt_tokens + it.completion_tokens;
              return (
                <TableRow key={`${it.name}-${i}`} className="border-b border-border/40">
                  <TableCell className="pl-0">
                    <div className="flex items-center gap-2">
                      <span className="h-2 w-2 rounded-full" style={{background: CHART_COLORS[i % CHART_COLORS.length]}} />
                      <span className="max-w-[140px] truncate text-xs font-medium">{it.name || t('metric.unknown')}</span>
                    </div>
                    <div className="mt-1.5 h-1 w-full max-w-[140px] overflow-hidden rounded-full bg-border">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${(tokens / max) * 100}%`,
                          background: CHART_COLORS[i % CHART_COLORS.length],
                        }}
                      />
                    </div>
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtNumber(it.requests)}</TableCell>
                  <TableCell className="text-xs tabular-nums">{fmtCompact(tokens)}</TableCell>
                  <TableCell className="pr-0 text-xs tabular-nums">
                    {it.credit > 0 ? fmtCredit(it.credit) : <span className="text-muted-foreground/70">—</span>}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      ) : (
        <EmptyState
          icon={Activity}
          title={t('stats.emptyTitle')}
          description={t('stats.emptyDesc')}
          className="flex flex-col items-center justify-center py-10 text-center"
        />
      )}
    </div>
  );
}
