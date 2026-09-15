'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {Users, CircleCheck, TriangleAlert, Activity, Server, Coins} from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {accountApi, statsApi, upstreamApi} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import type {Account, StatsSummary, UpstreamStatus, UsagePoint} from '@/lib/types';
import {expiryBarPercent, expiryVisual, fmtCompact, fmtNumber, fmtRemain} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {notify} from '@/lib/toast';

export default function DashboardPage() {
  const {realm, label: realmName} = useRealm();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [summary, setSummary] = useState<StatsSummary | null>(null);
  const [daily, setDaily] = useState<UsagePoint[]>([]);
  const [upstream, setUpstream] = useState<UpstreamStatus | null>(null);
  /** 实时积分（按 uid），叠加到 accounts 上；上游 /status 的 credits 可能滞后数小时 */
  const [liveCredits, setLiveCredits] = useState<Record<string, number>>({});

  // 切换版本后要重新取实时积分：两个版本的账号池不同，credits 也不能混
  const load = useCallback(async () => {
    const results = await Promise.allSettled([
      accountApi.list(),
      statsApi.summary(),
      statsApi.daily(14),
      upstreamApi.status(),
      // force=false：命中服务端 60 秒缓存，30 秒轮询不会反复打腾讯
      accountApi.refreshCredits(false),
    ]);
    if (results[0].status === 'fulfilled') setAccounts(results[0].value.accounts);
    if (results[1].status === 'fulfilled') setSummary(results[1].value);
    if (results[2].status === 'fulfilled') setDaily(results[2].value);
    if (results[3].status === 'fulfilled') setUpstream(results[3].value);
    if (results[4].status === 'fulfilled') {
      const r = results[4].value;
      setLiveCredits(
        Object.fromEntries(
          Object.entries(r.credits).filter(([, v]) => typeof v === 'number') as [string, number][],
        ),
      );
    }
    if (results.slice(0, 4).some((r) => r.status === 'rejected')) notify.err('部分数据加载失败');
  }, [realm]);

  useEffect(() => {
    load();
  }, [load]);

  // 账号健康度与用量会持续变化，用心跳刷新避免展示陈旧数据
  useHeartbeat(load, 30000);

  /**
   * 按当前版本过滤。
   *
   * 账号池里两种版本的账号都有，不过滤的话切到国际版仍会看到国内版的
   * 账号数、积分与健康快照（用户反馈过这个问题）。realm 为空的存量账号
   * 视为国内版，与后端判定一致。
   */
  const scoped = useMemo(
    () => accounts.filter((a) => (a.realm ?? 'cn') === realm),
    [accounts, realm],
  );

  const valid = scoped.filter((a) => !a.is_expired).length;
  const expiring = scoped.filter((a) => a.remain_seconds > 0 && a.remain_seconds < 3600).length;
  // 积分余额合计（仅统计已同步到的账号）
  // 优先用实时查询到的积分，其次上游 /status 的（可能滞后的）值
  const credOf = (a: Account) => liveCredits[a.uid] ?? a.credits;
  const creditsKnown = scoped.filter((a) => typeof credOf(a) === 'number');
  const totalCredits = creditsKnown.reduce((sum, a) => sum + (credOf(a) || 0), 0);
  const creditsLow = creditsKnown.filter((a) => (credOf(a) || 0) < 200).length;

  /**
   * 「反代上游」面板按版本重算。
   *
   * 上游 /status 返回的是**整个账号池**（含两个版本），它的 healthy/cooling/
   * disabled 是全局计数，直接用会在国际版视图下显示国内版的账号数。
   * /status 的每个账号条目带 realm，因此这里按版本自己统计。
   */
  const pool = useMemo(() => {
    const items = (upstream?.accounts || []) as Record<string, unknown>[];
    const mine = items.filter((it) => {
      const r = String((it as {realm?: string}).realm || 'cn');
      return r === realm;
    });
    return {
      total: mine.length,
      healthy: mine.filter((it) => Boolean((it as {healthy?: boolean}).healthy)).length,
      cooling: mine.filter((it) => Boolean((it as {cooling?: boolean}).cooling)).length,
      disabled: mine.filter((it) => Boolean((it as {disabled?: boolean}).disabled)).length,
      /** 上游是否返回了账号明细——没返回时上面几个数不可信，界面要说明 */
      known: items.length > 0,
    };
  }, [upstream, realm]);

  const chartData = daily.map((d) => ({
    day: d.day.slice(5),
    requests: d.requests,
    tokens: d.prompt_tokens + d.completion_tokens,
  }));

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      {/* 本页 30 秒自动刷新，且没有任何会改变数据的操作，
          因此不再放手动刷新按钮（移动端还省下一行） */}
      <PageHeader
        title="仪表盘"
        description={`${realmName}账号池健康度、反代网关与今日用量总览（每 30 秒自动刷新）`}
      />

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5 md:gap-4">
        <StatCard
          label="账号总数"
          value={fmtNumber(scoped.length)}
          hint={`${realmName}已纳管`}
          icon={Users}
          tone="neutral"
          delay={0}
        />
        <StatCard
          label="有效期内"
          value={fmtNumber(valid)}
          hint={valid === scoped.length ? '全部正常' : `${scoped.length - valid} 个异常`}
          icon={CircleCheck}
          tone="success"
          hintTone={valid === scoped.length ? 'success' : 'warning'}
          delay={0.05}
        />
        <StatCard
          label="即将过期"
          value={fmtNumber(expiring)}
          hint={expiring > 0 ? '<1h 需刷新' : '暂无风险'}
          icon={TriangleAlert}
          tone={expiring > 0 ? 'warning' : 'success'}
          hintTone={expiring > 0 ? 'warning' : 'neutral'}
          delay={0.1}
        />
        <StatCard
          label="积分余额"
          value={creditsKnown.length ? fmtNumber(totalCredits) : '—'}
          hint={
            !creditsKnown.length
              ? '等待上游同步'
              : creditsLow > 0
                ? `${creditsLow} 个账号低于 200`
                : `覆盖 ${creditsKnown.length} 个账号`
          }
          icon={Coins}
          tone={!creditsKnown.length ? 'neutral' : creditsLow > 0 ? 'warning' : 'accent'}
          hintTone={creditsLow > 0 ? 'warning' : undefined}
          delay={0.15}
        />
        <StatCard
          label="今日 Token"
          value={fmtCompact(summary?.today_tokens)}
          hint={`${fmtNumber(summary?.today_requests)} 次请求 · 含两种版本`}
          icon={Activity}
          tone="info"
          delay={0.2}
        />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-medium">近 14 天调用趋势</div>
            {/* 调用记录是全局的（不按版本拆分），如实标注而不是假装已过滤 */}
            <div className="text-[11px] text-muted-foreground">请求数 · 含两种版本</div>
          </div>
          <div className="h-[220px] w-full">
            {chartData.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{top: 4, right: 8, bottom: 0, left: -16}}>
                  <defs>
                    <linearGradient id="gReq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
                      <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" />
                  <YAxis tickLine={false} axisLine={false} fontSize={11} stroke="var(--muted-foreground)" />
                  <Tooltip
                    contentStyle={{
                      background: 'var(--popover)',
                      border: '1px solid var(--border)',
                      borderRadius: 12,
                      fontSize: 12,
                    }}
                  />
                  <Area
                    type="monotone"
                    dataKey="requests"
                    name="请求数"
                    stroke="var(--chart-1)"
                    fill="url(#gReq)"
                    strokeWidth={2}
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="grid h-full place-items-center text-xs text-muted-foreground">暂无调用数据</div>
            )}
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            反代上游
          </div>
          {upstream ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted-foreground">连接状态</span>
                {upstream.connected ? (
                  <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                    ● 正常
                  </Badge>
                ) : (
                  <Badge variant="destructive" className="rounded-full">
                    ● 不可用
                  </Badge>
                )}
              </div>
              {([
                // 账号类计数按当前版本重算；粘性会话与 Redis 无版本之分，保持全局
                ['健康账号', pool.known ? pool.healthy : '—'],
                ['冷却中', pool.known ? pool.cooling : '—'],
                ['已禁用', pool.known ? pool.disabled : '—'],
                ['粘性会话', upstream.sticky_sessions ?? 0],
                ['Redis 模式', upstream.redis_mode ?? '—'],
              ] as [string, string | number][]).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">{k}</span>
                  <span className="font-medium tabular-nums">{String(v)}</span>
                </div>
              ))}
              {upstream.error && <p className="text-[11px] text-red-500">{upstream.error}</p>}
            </div>
          ) : (
            <div className="grid h-[160px] place-items-center text-xs text-muted-foreground">未获取到上游状态</div>
          )}
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 text-sm font-medium">账号健康快照</div>
        {scoped.length ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {scoped.slice(0, 9).map((a) => {
              const pct = expiryBarPercent(a.remain_seconds, a.ttl_seconds);
              const vis = expiryVisual(a.remain_seconds);
              return (
                <div key={a.file} className="rounded-2xl bg-background/60 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={
                        'truncate text-sm font-medium ' +
                        (vis.tier === 'expired' ? 'text-muted-foreground' : '')
                      }
                    >
                      {a.nickname || a.uid}
                    </span>
                    <span className={'shrink-0 text-[10px] font-medium ' + vis.textClass}>
                      {vis.label}
                    </span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{width: `${pct}%`, background: vis.barColor}}
                    />
                  </div>
                  <div className="mt-1.5 flex items-center justify-between gap-2">
                    <span className={'text-[11px] tabular-nums ' + vis.textClass}>
                      {fmtRemain(a.remain_seconds)}
                    </span>
                    <span
                      className={
                        'text-[11px] font-medium tabular-nums ' +
                        (typeof credOf(a) !== 'number'
                          ? 'text-muted-foreground'
                          : (credOf(a) as number) <= 0
                            ? 'text-red-600 dark:text-red-400'
                            : (credOf(a) as number) < 200
                              ? 'text-amber-600 dark:text-amber-400'
                              : 'text-foreground')
                      }
                      title="积分余额"
                    >
                      {typeof credOf(a) === 'number' ? `${fmtNumber(credOf(a))} 积分` : ''}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={Users}
            title="暂无账号"
            description="点击底栏「快速添加」扫码授权腾讯账号"
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </section>
    </div>
  );
}
