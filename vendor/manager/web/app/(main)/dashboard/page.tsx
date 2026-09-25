'use client';

import {useEffect, useMemo, useState} from 'react';
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
import {useAsyncAll} from '@/lib/use-async-data';
import {getExpiryDailyGroup, groupExpiriesByDay} from '@/lib/display-prefs';
import {accountApi, statsApi, upstreamApi} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import type {
  Account,
  CreditExpiry,
  CreditsMeta,
  StatsSummary,
  UpstreamStatus,
  UsagePoint,
} from '@/lib/types';
import {
  expiryBarPercent,
  expiryVisual,
  fmtCompact,
  fmtDateTime,
  fmtNumber,
  fmtRemain,
} from '@/lib/format';
import {
  availabilityLabelKey,
  availabilityTitleKey,
  availabilityClass,
  availabilityOf,
  isDegraded,
  mergePoolStatus,
  type AvailabilityTier,
} from '@/lib/account-status';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {StatCard} from '@/components/common/layout/StatCard';
import {CreditCountdown} from '@/components/common/accounts/CreditCountdown';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {Badge} from '@/components/ui/badge';
import {Skeleton} from '@/components/ui/skeleton';
import {cn} from '@/lib/utils';
import {useT} from '@/lib/i18n/provider';

/**
 * 取数完成前的空值。
 *
 * 必须是模块级的同一份：写成 `values.accounts?.accounts ?? []` 的话，每次渲染
 * 都会新建一个数组，而它们要进下游 useMemo 的依赖——依赖每帧都变，那几个
 * memo 就等于没写。共享只读常量才稳定。
 */
const EMPTY_ACCOUNTS: Account[] = [];
const EMPTY_POINTS: UsagePoint[] = [];
const EMPTY_CREDITS: Record<string, CreditsMeta> = {};
const EMPTY_LIVE_CREDITS: Record<string, number> = {};

export default function DashboardPage() {
  const {realm, label: realmName} = useRealm();
  const t = useT();

  /**
   * 本页要的 5 份数据一次并发取回，成败逐项独立。
   *
   * 依赖是 realm：切换版本后**必须重取**——两个版本的账号池不同，credits 也
   * 不能混。useAsyncAll 在依赖变化时会连同旧数据一起清掉，否则会出现
   * 「标题已经写着国际版、卡片上还是国内版的数字」。
   *
   * 加载态 / 失败态 / 是否正在刷新也由它给出，本页不再自己维护这几个 useState。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    accounts: () => accountApi.list(),
    summary: () => statsApi.summary(realm),
    daily: () => statsApi.daily(14, realm),
    upstream: () => upstreamApi.status(),
    // force=false：命中服务端 60 秒缓存，30 秒轮询不会反复打腾讯
    credits: () => accountApi.refreshCredits(false),
  }, [realm]);

  const accounts: Account[] = values.accounts?.accounts ?? EMPTY_ACCOUNTS;
  const summary: StatsSummary | null = values.summary ?? null;
  const daily: UsagePoint[] = values.daily ?? EMPTY_POINTS;
  const upstream: UpstreamStatus | null = values.upstream ?? null;
  /** 实时积分（按 uid），叠加到 accounts 上；上游 /status 的 credits 可能滞后数小时 */
  const liveCredits = useMemo(() => {
    const raw = values.credits?.credits;
    if (!raw) return EMPTY_LIVE_CREDITS;
    return Object.fromEntries(
      Object.entries(raw).filter(([, v]) => typeof v === 'number') as [string, number][],
    );
  }, [values.credits]);
  /** 各账号的积分套餐到期时间（按 uid），与实时积分同一次查询返回 */
  const creditsMeta: Record<string, CreditsMeta> = values.credits?.meta ?? EMPTY_CREDITS;

  // 账号健康度与用量会持续变化，用心跳刷新避免展示陈旧数据。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——
  // 否则骨架会每 30 秒闪一次，用户以为页面在抽风。
  useHeartbeat(reload, 30000);

  /** 有字段没刷新成功。此时至少还有一份数据在，用顶部提示如实说明，不清空内容 */
  const partialFailed = Object.keys(errors).length > 0;

  /**
   * 合并上游池状态 + 按当前版本过滤。
   *
   * 只保留**一份**数组（`scoped`）：合并结果的每个字段都是原始字段的超集
   * （`mergePoolStatus` 用 `...a` 展开），没有第二个数组就没有「两个数组说的
   * 不一样」这种可能——而这次要修的正是「两处各自推导同一个事实」。
   *
   * 健康快照此前直接拿 `expiryVisual()` 的 label 当状态标签，而那个分档讲的是
   * **Token 有效期**——于是只要令牌没过期就显示「在线」，账号在不在上游池里、
   * 有没有被禁用冷却一概没看。结果同一个账号首页说「在线」、账号页说「未加载」
   * （用户报的就是这个）。现在两页共用 `lib/account-status`。
   *
   * 版本过滤：账号池里两种版本的账号都有，不过滤的话切到国际版仍会看到国内版的
   * 账号数、积分与健康快照。realm 为空的存量账号视为国内版，与后端判定一致。
   */
  const scoped = useMemo(
    () => mergePoolStatus(accounts, upstream).filter((a) => (a.realm ?? 'cn') === realm),
    [accounts, upstream, realm],
  );

  /** 可用性分档的汇总（只统计「能正常调用」的，与账号页说法一致） */
  const availability = useMemo(() => {
    const counts: Record<AvailabilityTier, number> = {
      disabled: 0, disabledByPanel: 0, manualDisabled: 0, expired: 0, unknown: 0, cooling: 0,
      neverSucceeded: 0, notLoaded: 0, online: 0,
    };
    for (const a of scoped) counts[availabilityOf(a)] += 1;
    return counts;
  }, [scoped]);

  /**
   * 「不可用」的账号数：未加载 / 已禁用 / 一直失败 / 冷却中——这些是**用户需要
   * 处理**的（去上游重载、重新登录、换模型等），而不是单纯「令牌快过期」。
   * 与快照卡片的分档同源，所以卡片提示与快照不会互相打架。
   *
   * 两类**刻意不计入**：
   *   · unknown（读不到上游）——那是我们看不到，不是账号有问题；
   *   · disabledByPanel / manualDisabled（主动停用）——那是用户自己的决定，
   *     不需要「处理」，计进去会让卡片一直提示「N 个不可用」而用户其实已经
   *     处理完了（两种机制同属这一类，只是实现不同）。
   */
  const unusable = availability.notLoaded + availability.disabled
    + availability.neverSucceeded + availability.cooling;

  /**
   * 连败降权计数（上游 issue #114）。**上游把它并进 cooling**，所以 realm_totals
   * 里的「冷却中」是个混数：既有等一会儿就好的限流退避，也有「这个号在持续失败」
   * 的降权。面板只显示混数时，用户看不出降权的存在（反馈原话：
   * 「降权统计这里根本不统计」）。这里从账号明细单独数一份，与上游口径不冲突
   * ——它只是把 cooling 里的降权那一部分显式化。
   */
  const degraded = scoped.filter(isDegraded).length;

  const valid = scoped.filter((a) => !a.is_expired).length;
  const expiring = scoped.filter((a) => a.remain_seconds > 0 && a.remain_seconds < 3600).length;
  // 积分余额合计（仅统计已同步到的账号）
  // 优先用实时查询到的积分，其次上游 /status 的（可能滞后的）值
  const credOf = (a: Account) => liveCredits[a.uid] ?? a.credits;
  const creditsKnown = scoped.filter((a) => typeof credOf(a) === 'number');
  const totalCredits = creditsKnown.reduce((sum, a) => sum + (credOf(a) || 0), 0);
  const creditsLow = creditsKnown.filter((a) => (credOf(a) || 0) < 200).length;
  /**
   * 最近一笔积分到期（全体账号里最早的那个到期时刻）。
   *
   * 为什么值得占首页一行：积分过期即作废且不可恢复，用户能采取的行动
   * （多跑点任务把它用掉）只在到期**之前**有效。汇总卡片此前只报总量，
   * 看不出「总量虽多但大部分下周就没了」这种情况。
   */
  const nextExpiry = useMemo(() => {
    let best: CreditExpiry | null = null;
    for (const m of Object.values(creditsMeta)) {
      const e = m.expiries?.[0];
      if (e && (!best || e.at < best.at)) best = e;
    }
    return best;
  }, [creditsMeta]);
  // 7 天内的到期算紧急（与倒计时胶囊的琥珀/红分档同一条线）。取渲染时刻即可：
  // 阈值是 7 天，而本页每 30 秒重渲染一次，几秒的偏差不影响分档。
  const expiryUrgent = !!nextExpiry && nextExpiry.at - Date.now() / 1000 < 7 * 86400;

  /** 「到期积分按天模糊统计」开关（界面偏好，存在浏览器本地）。
   *  首屏后读：直接读会让服务端渲染与客户端不一致（与 realm-context 同理）。 */
  const [dailyGroup, setDailyGroup] = useState(false);
  useEffect(() => {
    setDailyGroup(getExpiryDailyGroup());
  }, []);

  /**
   * 全体账号的到期明细（按时刻升序）。
   *
   * 与上面 nextExpiry 的区别：那个只看每个账号的**第一笔**，回答「最近一笔什么时候
   * 没了」；这里收全量，供「按天归并」用——同一天到期的多笔要能相加，
   * 只看第一笔永远算不出那一天的总和。
   */
  const allExpiries = useMemo(() => {
    const out: CreditExpiry[] = [];
    for (const m of Object.values(creditsMeta)) {
      for (const e of m.expiries ?? []) out.push(e);
    }
    return out.sort((a, b) => a.at - b.at);
  }, [creditsMeta]);

  /**
   * 卡片上实际要显示的到期条目。
   *
   * 关闭（默认）= 最近一笔；开启 = 按本地日历日归并后的第一条（最近那一天，
   * 金额是那天到期的**总和**）。两者数量级不同（一笔 vs 一天），所以开关默认
   * 关闭——升级后看到的还是原来那个数，想按天看的自己打开。
   */
  const expiryShown = useMemo(
    () => (dailyGroup
      ? groupExpiriesByDay(allExpiries)
      : (nextExpiry ? [nextExpiry] : [])),
    [dailyGroup, allExpiries, nextExpiry],
  );

  /**
   * 「反代上游」面板的计数，取自上游 `/status` 的 `realm_totals`。
   *
   * 走过的弯路记在这里，免得后人重蹈：
   *   1. 最初读 `upstream.healthy` —— 那是**全局**汇总，切到国际版会显示
   *      两个版本加起来的数；
   *   2. 于是改成从 `accounts` 明细里自己数，却读了明细条目上的 healthy ——
   *      而**账号明细里根本没有 healthy 字段**（它是汇总层才有的），布尔转换
   *      恒为 false，面板因此全显示 0。
   * 上游其实已经按版本分好组了（`realm_totals.cn` / `.global`），直接用即可——
   * 口径与上游状态机完全一致，也不用我们去猜 healthy 该怎么算。
   */
  const pool = useMemo(() => {
    const perRealm = upstream?.realm_totals?.[realm];
    if (perRealm) {
      return {
        total: perRealm.total,
        healthy: perRealm.healthy,
        cooling: perRealm.cooling,
        disabled: perRealm.disabled,
        known: true,
      };
    }
    // 上游未提供 realm_totals（老版本）时退回顶层汇总，并如实说明是全局口径
    const hasTop = typeof upstream?.total === 'number';
    return {
      total: upstream?.total ?? 0,
      healthy: upstream?.healthy ?? 0,
      cooling: upstream?.cooling ?? 0,
      disabled: upstream?.disabled ?? 0,
      known: hasTop,
      /** true = 只能用全局汇总（含两个版本），界面需标注 */
      globalOnly: hasTop,
    };
  }, [upstream, realm]);

  const chartData = daily.map((d) => ({
    day: d.day.slice(5),
    requests: d.requests,
    tokens: d.prompt_tokens + d.completion_tokens,
    // 失败数与请求数是**两条独立的数据来源**（前者取自请求日志，后者取自用量
    // 汇总），所以曲线不会互相覆盖：某天「requests=0 但 failed=31」是完全
    // 可能的形态（全池中断），图上看得出「那天出过事」而不是「没有请求」。
    failed: d.failed ?? 0,
  }));

  /** 今日失败总数（4xx + 5xx）。用于今天那张卡片的提示与色调。 */
  const todayFailed =
    (summary?.failures?.today_4xx ?? 0) + (summary?.failures?.today_5xx ?? 0);
  const weekFailed =
    (summary?.failures?.week_4xx ?? 0) + (summary?.failures?.week_5xx ?? 0);

  /**
   * 页头。两条渲染路径（首屏 / 已就绪）都要用，抽出来免得改一处漏一处。
   * 本页 30 秒自动刷新，且没有任何会改变数据的操作，因此不放手动刷新按钮
   * （移动端还省下一行）。
   */
  const header = (
    <PageHeader
      title={t('dashboard.title')}
      description={t('dashboard.description', {realm: realmName})}
    />
  );

  // 首屏：还没拿到任何数据。此时**不能**渲染下面的卡片——它们会把「还没取到」
  // 显示成「暂无账号」「暂无调用数据」，那是在撒谎。一次都没取到（失败）同理：
  // 页面上没有任何可信数字，直接给错误态与「重试」，而不是一直转圈。
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <DashboardSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 刷新时有请求失败：常驻提示，**不**顶掉已经显示出来的内容——那些数据
          仍然是对的，只是可能不是最新的。原先这里只弹一个 toast，几秒后自己
          消失，用户切回来看到的是一片「正常」的数字。 */}
      {partialFailed && (
        <LoadError message={t('dashboard.partialLoadFailed')} onRetry={reload} />
      )}

      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5 md:gap-4">
        <StatCard
          label={t('dashboard.totalAccounts')}
          value={fmtNumber(scoped.length)}
          hint={t('dashboard.totalAccountsHint', {realm: realmName})}
          icon={Users}
          tone="neutral"
          delay={0}
        />
        <StatCard
          label={t('dashboard.valid')}
          value={fmtNumber(valid)}
          hint={
            // 提示优先级：先说**不可用**（能真用的少了一个，需要处理），
            // 再说令牌快过期，最后才是「全部正常」。
            //
            // 此前只看 `valid === scoped.length`（令牌都在有效期内）就说
            // 「全部正常」——于是出现「卡片说全部正常、快照里却有账号未加载」的
            // 矛盾。令牌有效不等于账号可用：它可能没进上游池、被禁用或一直在
            // 失败。口径与快照卡片、账号页完全一致（同一套 availabilityOf）。
            unusable > 0
              ? t('dashboard.unusable', {count: unusable, n: unusable})
              : valid === scoped.length
                ? t('dashboard.allOk')
                : t('dashboard.abnormal', {count: scoped.length - valid, n: scoped.length - valid})
          }
          icon={CircleCheck}
          tone={unusable > 0 ? 'warning' : 'success'}
          hintTone={unusable > 0 ? 'warning' : (valid === scoped.length ? 'success' : 'warning')}
          delay={0.05}
        />
        <StatCard
          label={t('expiry.urgent')}
          value={fmtNumber(expiring)}
          hint={expiring > 0 ? t('dashboard.needRefresh') : t('dashboard.noRisk')}
          icon={TriangleAlert}
          tone={expiring > 0 ? 'warning' : 'success'}
          hintTone={expiring > 0 ? 'warning' : 'neutral'}
          delay={0.1}
        />
        <StatCard
          label={t('metric.credits')}
          // 值里带上最近一笔到期：额度高但下周作废，比额度低更值得注意
          value={
            <span className="inline-flex items-baseline gap-1.5">
              <span>{creditsKnown.length ? fmtNumber(totalCredits) : '—'}</span>
              <CreditCountdown expiries={expiryShown} />
            </span>
          }
          hint={
            // 优先级：先报「余额快见底」（这是原有的告警，不能被新信息顶掉），
            // 其次报最近一笔到期（具体日期与金额，只有这里能看到），
            // 最后才是「N 个账号余额已知」这种平淡的状态说明。
            !creditsKnown.length
              ? t('dashboard.waitingUpstream')
              : creditsLow > 0
                ? t('dashboard.creditsLow', {count: creditsLow, n: creditsLow})
                : expiryShown.length
                  ? t(dailyGroup ? 'dashboard.creditsExpiryDaily' : 'dashboard.creditsExpiry', {
                      time: fmtDateTime(expiryShown[0].at),
                      amount: fmtNumber(expiryShown[0].amount),
                    })
                  : t('dashboard.creditsCovered', {count: creditsKnown.length, n: creditsKnown.length})
          }
          icon={Coins}
          tone={
            !creditsKnown.length ? 'neutral' : creditsLow > 0 || expiryUrgent ? 'warning' : 'accent'
          }
          hintTone={creditsLow > 0 || expiryUrgent ? 'warning' : undefined}
          delay={0.15}
        />
        <StatCard
          label={t('dashboard.todayTokens')}
          value={fmtCompact(summary?.today_tokens)}
          hint={
            // 有失败时**优先说失败**：这个数字是用户排查问题的入口，而
            // 「今天 N 次请求」是背景信息。没失败才显示请求数。
            todayFailed > 0
              ? t('dashboard.todayFailed', {n: fmtNumber(todayFailed)})
              : t('dashboard.todayRequests', {
                  n: fmtNumber(summary?.today_requests),
                  realm: realmName,
                })
          }
          icon={Activity}
          tone={todayFailed > 0 ? 'warning' : 'info'}
          hintTone={todayFailed > 0 ? 'warning' : undefined}
          delay={0.2}
        />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-medium">{t('dashboard.trend14')}</div>
            {/* 按当前版本过滤（与统计页同一口径），不再混两个版本。
                近 7 天有失败时在这里点出总数——用户看曲线只能看出"某天有虚线"，
                具体多少次要悬停；给个总数省这一步。 */}
            <div className="text-[11px] text-muted-foreground">
              {weekFailed > 0
                ? t('dashboard.weekFailed', {n: fmtNumber(weekFailed), realm: realmName})
                : t('dashboard.requestsByRealm', {realm: realmName})}
            </div>
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
                    name={t('metric.requests')}
                    stroke="var(--chart-1)"
                    fill="url(#gReq)"
                    strokeWidth={2}
                  />
                  {/* 失败曲线单独画（虚线、不发散填充）：它来自另一份数据源，
                      与请求数不是「同一条曲线的两个部分」，不该堆叠。
                      没有失败时这条线贴着 0，不干扰读数。 */}
                  <Area
                    type="monotone"
                    dataKey="failed"
                    name={t('dashboard.failedRequests')}
                    stroke="var(--destructive)"
                    fill="none"
                    strokeWidth={1.5}
                    strokeDasharray="4 3"
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="grid h-full place-items-center text-xs text-muted-foreground">{t('dashboard.noCallData')}</div>
            )}
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('dashboard.upstreamPanel')}
          </div>
          {upstream ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted-foreground">{t('dashboard.connStatus')}</span>
                {upstream.connected ? (
                  <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                    {t('dashboard.connected')}
                  </Badge>
                ) : (
                  <Badge variant="destructive" className="rounded-full">
                    {t('dashboard.unavailable')}
                  </Badge>
                )}
              </div>
              {([
                // 账号类计数按当前版本（上游 realm_totals）；粘性会话与 Redis
                // 无版本之分，保持全局
                [t('dashboard.healthyAccounts'), pool.known ? pool.healthy : '—'],
                [t('dashboard.cooling'), pool.known ? pool.cooling : '—'],
                [t('dashboard.degraded'), pool.known ? degraded : '—'],
                [t('dashboard.disabled'), pool.known ? pool.disabled : '—'],
                [t('dashboard.stickySessions'), upstream.sticky_sessions ?? 0],
                [t('dashboard.redisMode'), upstream.redis_mode ?? '—'],
              ] as [string, string | number][]).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">{k}</span>
                  <span className="font-medium tabular-nums">{String(v)}</span>
                </div>
              ))}
              {pool.globalOnly && !upstream.error && (
                <p className="text-[11px] text-muted-foreground">
                  {t('dashboard.globalTotalsNote')}
                </p>
              )}
              {upstream.error && <p className="text-[11px] text-red-500">{upstream.error}</p>}
            </div>
          ) : (
            <div className="grid h-[160px] place-items-center text-xs text-muted-foreground">{t('dashboard.noUpstreamStatus')}</div>
          )}
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-sm font-medium">{t('dashboard.healthSnapshot')}</span>
          {/* 各档汇总：一眼看清「有几个能真用」。文案与账号页共用同一套键，
              两页不会出现「一个叫在线、一个叫正常」这种漂移。 */}
          {(['online', 'notLoaded', 'cooling', 'neverSucceeded', 'disabled', 'expired', 'unknown'] as AvailabilityTier[])
            .filter((tier) => availability[tier] > 0)
            .map((tier) => (
              <span
                key={tier}
                className={`text-[11px] tabular-nums ${availabilityClass(tier)}`}
                title={availabilityTitleKey(tier) ? t(availabilityTitleKey(tier)!) : undefined}
              >
                {t(availabilityLabelKey(tier, scoped.find((a) => availabilityOf(a) === tier)))}
                {' '}
                {availability[tier]}
              </span>
            ))}
        </div>
        {scoped.length ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {scoped.slice(0, 9).map((a) => {
              const pct = expiryBarPercent(a.remain_seconds, a.ttl_seconds);
              const vis = expiryVisual(a.remain_seconds);
              // 状态标签用**可用性**分档，而不是 Token 有效期分档：
              // 后者只有「有效期内」这一个概念，会把不在池里/被禁用的账号
              // 也写成「在线」（与账号页冲突）。有效期信息仍由下面的进度条
              // 与剩余天数如实呈现，两者不再混在一起说。
              const tier = availabilityOf(a);
              const statusLabel = t(availabilityLabelKey(tier, a));
              const titleKey = availabilityTitleKey(tier);
              return (
                <div key={a.file} className="rounded-2xl bg-background/60 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className={
                        'truncate text-sm font-medium ' +
                        (tier === 'expired' ? 'text-muted-foreground' : '')
                      }
                    >
                      {a.nickname || a.uid}
                    </span>
                    <span
                      className={'shrink-0 text-[10px] font-medium ' + availabilityClass(tier)}
                      title={titleKey ? t(titleKey) : undefined}
                    >
                      {statusLabel}
                    </span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{width: `${pct}%`, background: vis.barColor}}
                    />
                  </div>
                  <div className="mt-1.5 flex items-center justify-between gap-2">
                    <span className={'text-[11px] tabular-nums ' + vis.textClass} title={t('accounts.colExpiry')}>
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
                      title={t('metric.credits')}
                    >
                      {typeof credOf(a) === 'number'
                        ? t('metric.creditAmount', {n: fmtNumber(credOf(a))})
                        : ''}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState
            icon={Users}
            title={t('dashboard.noAccounts')}
            description={t('dashboard.noAccountsHint')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </section>
    </div>
  );
}

/**
 * 骨架里的一根「条」。
 *
 * 为什么要显式指定底色：`Skeleton` 自带 `bg-accent`，而本页所有卡片用的是
 * `bg-muted` —— 这两个颜色在 globals.css 里**明暗两套主题下都是同一个字面量**
 * （浅色同为 oklch(0.967 0.001 286.375)，深色同为 oklch(0.274 0.006 286.033)），
 * 直接放上去等于画了看不见的条。改用前景色的低透明度：浅色下压暗、深色下提亮，
 * 两种主题都能看出形状。
 */
function Bar({className}: {className?: string}) {
  return <Skeleton className={cn('bg-foreground/10', className)} />;
}

/** 统计卡片的骨架。外壳与 StatCard 逐项对齐（同高、同圆角、同底色），到位时不跳 */
function StatCardSkeleton() {
  return (
    <div className="min-h-[88px] rounded-[20px] bg-muted px-3.5 py-3 sm:min-h-[96px] sm:px-4">
      <div className="flex items-start justify-between gap-2">
        <Bar className="h-2.5 w-16" />
        <Bar className="h-6 w-6 rounded-full" />
      </div>
      <Bar className="mt-3 h-6 w-20" />
      <Bar className="mt-2 h-2.5 w-24" />
    </div>
  );
}

/**
 * 首屏骨架。
 *
 * 结构与真实内容**逐块对应**（五张卡片 / 趋势图 + 上游面板 / 健康快照），而不是
 * 一坨居中的转圈：数据到位时版面不会整体跳一下，加载期间也不会看起来像空白页。
 * 页面每 30 秒心跳刷新一次，但只有「一次都没取到」才会走到这里（见
 * use-async-data 的 isInitialLoading），所以不会一闪一闪。
 */
function DashboardSkeleton() {
  return (
    <>
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-5 md:gap-4">
        {Array.from({length: 5}, (_, i) => <StatCardSkeleton key={i} />)}
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <Bar className="h-3.5 w-28" />
            <Bar className="h-3 w-20" />
          </div>
          <Bar className="h-[220px] w-full rounded-2xl" />
        </div>
        <div className="rounded-[20px] bg-muted p-4">
          <Bar className="mb-3 h-3.5 w-20" />
          <div className="space-y-3">
            {Array.from({length: 6}, (_, i) => (
              <div key={i} className="flex items-center justify-between">
                <Bar className="h-2.5 w-16" />
                <Bar className="h-2.5 w-8" />
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="rounded-[20px] bg-muted p-4">
        <Bar className="mb-3 h-3.5 w-24" />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({length: 6}, (_, i) => (
            <div key={i} className="rounded-2xl bg-background/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <Bar className="h-3.5 w-20" />
                <Bar className="h-2.5 w-10" />
              </div>
              <Bar className="mt-2 h-1.5 w-full rounded-full" />
              <div className="mt-1.5 flex items-center justify-between gap-2">
                <Bar className="h-2.5 w-12" />
                <Bar className="h-2.5 w-10" />
              </div>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
