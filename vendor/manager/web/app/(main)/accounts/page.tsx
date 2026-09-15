'use client';

import Link from 'next/link';
import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Gift,
  Zap,
  KeyRound,
  Trash2,
  Plus,
  Users,
  Power,
  CalendarCheck,
  Coins,
  ChevronRight,
} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {accountApi, upstreamApi, errText} from '@/lib/api';
import type {Account, CreditsMeta, UpstreamStatus} from '@/lib/types';
import {expiryBarPercent, expiryVisual, fmtAgo, fmtDateTime, fmtNumber, fmtRemain} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {AddAccountDialog} from '@/components/common/accounts/AddAccountDialog';
import {useAuth} from '@/lib/auth-context';
import {realmLabel, useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

export default function AccountsPage() {
  const {realm, label: realmName} = useRealm();
  const {isAdmin} = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [upstream, setUpstream] = useState<UpstreamStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [busyFile, setBusyFile] = useState<string | null>(null);
  const [checkinAllBusy, setCheckinAllBusy] = useState(false);
  /** 每个账号积分是实时查询还是命中缓存（含缓存已存在秒数） */
  const [creditsMeta, setCreditsMeta] = useState<Record<string, CreditsMeta>>({});
  /**
   * 查到的积分按 uid 单独存一份，渲染时再叠加到账号上。
   * 不能直接改写 accounts：积分请求与账号列表是并发的，
   * 积分常常先返回，那时 accounts 还是空的，就地改写会落空。
   */
  const [liveCredits, setLiveCredits] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    setLoading(true);
    const [accRes, upRes] = await Promise.allSettled([
      accountApi.list(),
      upstreamApi.status(),
    ]);
    if (accRes.status === 'fulfilled') setAccounts(accRes.value.accounts);
    else notify.err(errText(accRes.reason));
    if (upRes.status === 'fulfilled') setUpstream(upRes.value);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 打开页面时自动拉一次实时积分：上游 /status 的 credits 可能滞后数小时，
  // 首次进入应展示真实余额。服务端有 TTL 缓存，重复进入不会频繁请求。
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // force=false：60 秒内重复打开页面直接命中服务端缓存，
        // 不再每次都全量请求腾讯；命中时界面会明确标注「缓存」
        const r = await accountApi.refreshCredits(false);
        if (!alive) return;
        setLiveCredits(
          Object.fromEntries(
            Object.entries(r.credits).filter(([, v]) => typeof v === 'number') as [string, number][],
          ),
        );
        setCreditsMeta(r.meta ?? {});
      } catch {
        /* 静默失败：仍显示上游缓存值 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // 上游状态（冷却 / 成功计数等）会随时间变化，页面停留时定时刷新，
  // 否则会一直显示打开页面那一刻的旧数据。
  useHeartbeat(load, 30000);

  /** 刷新所有账号的实时积分（直接向腾讯查询，非上游缓存值） */
  const [creditsBusy, setCreditsBusy] = useState(false);
  const refreshCredits = useCallback(async () => {
    setCreditsBusy(true);
    try {
      const r = await accountApi.refreshCredits(true);
      setLiveCredits(
        Object.fromEntries(
          Object.entries(r.credits).filter(([, v]) => typeof v === 'number') as [string, number][],
        ),
      );
      setCreditsMeta(r.meta ?? {});
      if (r.failed.length === 0) {
        notify.ok('积分已刷新', `${r.succeeded}/${r.total} 个账号`);
      } else {
        notify.warn('部分账号积分未取到', `${r.succeeded}/${r.total} 成功，其余见账号状态`);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCreditsBusy(false);
    }
  }, []);

  /** 批量签到：逐账号记录结果 */
  const checkinAll = useCallback(async () => {
    setCheckinAllBusy(true);
    try {
      const r = await accountApi.checkinAll();
      const failed = r.total - r.succeeded;
      if (r.total === 0) {
        notify.info('没有可签到的账号');
      } else if (failed === 0) {
        notify.ok(`全部签到完成`, `${r.succeeded}/${r.total} 个账号成功`);
      } else {
        notify.warn(`签到完成，${failed} 个失败`, `${r.succeeded}/${r.total} 个账号成功，详见「任务记录」页`);
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCheckinAllBusy(false);
    }
  }, [load]);

  /** 将本地 auths 文件与上游账号池状态按 uid 合并 */
  const merged = useMemo(() => {
    const pool = new Map<string, Record<string, unknown>>();
    for (const item of upstream?.accounts ?? []) {
      const uid = String((item as Record<string, unknown>).uid ?? (item as Record<string, unknown>).UID ?? '');
      if (uid) pool.set(uid, item as Record<string, unknown>);
    }
    return accounts.map((a) => {
      const p = pool.get(a.uid);
      if (!p) return a;
      return {
        ...a,
        healthy: typeof p.healthy === 'boolean' ? p.healthy : null,
        disabled: typeof p.disabled === 'boolean' ? p.disabled : null,
        disabled_reason: typeof p.disabled_reason === 'string' ? p.disabled_reason : '',
        in_flight: typeof p.in_flight === 'number' ? p.in_flight : null,
        cooling: typeof p.cooling === 'boolean' ? p.cooling : null,
        last_used: typeof p.last_used === 'number' ? p.last_used : null,
      } satisfies Account;
    });
  }, [accounts, upstream]);

  /**
   * 按当前版本过滤。
   *
   * 上游是单实例双版本共存，账号池里两种账号都有；不区分的话切到国际版
   * 仍会看到国内版账号（反之亦然），「切换」就没有意义了。
   * 存量账号没有 realm 字段，后端按域名回退（多为 cn），与升级前一致。
   */
  const visible = useMemo(
    () => merged.filter((a) => (a.realm ?? 'cn') === realm),
    [merged, realm],
  );

  /** 执行单账号操作（签到 / 测活 / 刷新 / 删除），成功后同步底栏计数 */
  async function run(file: string, fn: () => Promise<unknown>, okMsg: string) {
    setBusyFile(file);
    try {
      const res = (await fn()) as {message?: string; ok?: boolean; credits?: number | null};
      const ok = res.ok !== false;
      // 签到会返回刷新后的实时积分，直接就地更新，省一次请求
      if (typeof res.credits === 'number') {
        setAccounts((prev) => prev.map((a) => (a.file === file ? {...a, credits: res.credits} : a)));
      }
      (ok ? notify.ok : notify.err)(res.message || okMsg);
      await load();
      window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusyFile(null);
    }
  }

  /** 仅重启上游容器，不涉及单个账号，因此单独处理 */
  const [restarting, setRestarting] = useState(false);
  async function restartUpstream() {
    setRestarting(true);
    try {
      const res = await accountApi.restart();
      (res.ok ? notify.ok : notify.err)(res.message || '已重启上游');
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRestarting(false);
    }
  }

  /** 账号状态徽章（表格与移动端卡片共用） */
  function renderStatus(a: Account) {
    if (a.disabled === true) {
      // 上游对 11140（request illegal）是**硬禁用**、到期也不自愈，必须重新登录
      // 才能恢复；只说「已禁用」会让人干等。原因里含 11140/request illegal 时
      // 直接提示要重新授权。
      const reason = String(a.disabled_reason || '');
      const needRelogin = /11140|request illegal/i.test(reason);
      return (
        <Badge
          variant="destructive"
          className="rounded-full"
          title={reason ? `禁用原因：${reason}` : undefined}
        >
          ● {needRelogin ? '已禁用（需重新登录）' : '已禁用'}
        </Badge>
      );
    }
    if (a.is_expired) return <Badge variant="destructive" className="rounded-full">● 已过期</Badge>;
    if (a.cooling) {
      // 带上「还要等多久」：只写「冷却中」的话用户不知道是几秒还是几小时，
      // 只能反复刷新碰运气。剩余时间是上游状态机给的权威值。
      const secs = a.cool_remaining_sec;
      const left = typeof secs === 'number' && secs > 0 ? fmtRemain(secs) : '';
      const models = (a.rate_limited_models ?? []).map((m) => m.model);
      const tip = [
        left ? `预计 ${left}后恢复` : '',
        models.length ? `被限流的模型：${models.join('、')}` : '',
      ]
        .filter(Boolean)
        .join('\n');
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-amber-600 dark:text-amber-400"
          title={tip || undefined}
        >
          ● 冷却中{left ? ` · ${left}` : ''}
          {models.length > 1 && <span className="ml-1 opacity-70">（{models.length} 个模型）</span>}
        </Badge>
      );
    }
    return (
      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
        ● 在线
      </Badge>
    );
  }

  /** 积分余额 + 数据来源标注。
   *  明确区分「实时」与「缓存 x 秒前」，避免把滞后的数字当成刚查到的。 */
  function renderCredits(a: Account) {
    // 优先用刚查到的实时值，其次上游 /status 的缓存值
    const value = liveCredits[a.uid] ?? a.credits;
    if (value === null || value === undefined) {
      return (
        <span
          className="text-xs text-muted-foreground"
          title="上游尚未返回该账号的积分（可能是刚添加、或上游不可达）"
        >
          —
        </span>
      );
    }
    const meta = creditsMeta[a.uid];
    const tone =
      value <= 0
        ? 'text-red-600 dark:text-red-400'
        : value < 200
          ? 'text-amber-600 dark:text-amber-400'
          : 'text-foreground';
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className={`text-xs font-medium tabular-nums ${tone}`} title="当前可花费积分余额（所有套餐剩余额度合计）">
          {fmtNumber(value)}
        </span>
        {meta &&
          (meta.cached ? (
            <span
              className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-amber-600 dark:text-amber-400"
              title="60 秒内已查过，直接用了服务端缓存；点「刷新积分」可强制重新查询"
            >
              {meta.cache_age != null ? `缓存 ${meta.cache_age}s 前` : '缓存'}
            </span>
          ) : (
            <span
              className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-emerald-600 dark:text-emerald-400"
              title="刚刚向腾讯查询的实时值"
            >
              实时
            </span>
          ))}
      </span>
    );
  }

  /** Token 有效期进度条 */
  function renderExpiry(a: Account) {
    const pct = expiryBarPercent(a.remain_seconds, a.ttl_seconds);
    const vis = expiryVisual(a.remain_seconds);
    // 「有效期」是剩余时间，刷新会把它重新拉满，所以单看天数分不清
    // 「刚被保活续期」和「从没刷新过、还用着当初扫码的长令牌」。
    // 补一行签发时间（≈ 最近一次刷新）才能区分——后者是保活没覆盖到的隐患账号。
    const issued = a.issued_at ?? null;
    return (
      <div className="w-[150px]">
        <div className={`mb-1 text-[11px] font-medium tabular-nums ${vis.textClass}`}>
          {fmtRemain(a.remain_seconds)}
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-border">
          <div className="h-full rounded-full transition-all" style={{width: `${pct}%`, background: vis.barColor}} />
        </div>
        {issued != null && (
          <div
            className="mt-1 text-[10px] text-muted-foreground/70"
            title={`令牌签发于 ${fmtDateTime(issued)}（刷新会换发新令牌）`}
          >
            最后续期 {fmtAgo(issued)}
          </div>
        )}
      </div>
    );
  }

  /** 单账号操作按钮组 */
  function renderActions(a: Account) {
    const busy = busyFile === a.file;
    // 国际版没有签到体系（上游对 global 账号直接过滤，不发请求）。
    // 这一行的「签到」按钮对国际版账号只会返回「已跳过」，属误导，故不显示。
    const canCheckin = (a.realm ?? 'cn') === 'cn';
    return (
      <div className="flex justify-end gap-1">
        {canCheckin && (
          <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title="签到" disabled={busy}
            onClick={() => run(a.file, () => accountApi.checkin(a.file), '操作完成')}>
            <Gift className="h-3.5 w-3.5" />
          </Button>
        )}
        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title="连通性测试" disabled={busy}
          onClick={() => run(a.file, () => accountApi.test(a.file), '测试完成')}>
          <Zap className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title="刷新 Token" disabled={busy}
          onClick={() => run(a.file, () => accountApi.refresh(a.file), '刷新完成')}>
          <KeyRound className="h-3.5 w-3.5" />
        </Button>
        <ConfirmDialog
          title={`删除账号「${a.nickname || a.uid}」？`}
          description="将删除本地授权文件，并自动重载上游使其生效。此操作不可撤销。"
          confirmText="删除"
          destructive
          onConfirm={() => run(a.file, () => accountApi.remove(a.file), '已删除')}
          trigger={
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title="删除">
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          }
        />
      </div>
    );
  }

  /** 头像（首字母） */
  function renderAvatar(a: Account) {
    return (
      <div
        className={
          'grid h-7 w-7 shrink-0 place-items-center rounded-full text-[11px] font-semibold ' +
          (a.is_expired ? 'bg-muted-foreground/20 text-muted-foreground' : 'bg-primary text-primary-foreground')
        }
      >
        {(a.nickname || '?').charAt(0)}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="账号管理"
        description={
          realm === 'global'
            ? '腾讯 CodeBuddy 账号池（国际版）：Token 有效期与连通性（每 30 秒自动刷新）'
            : '腾讯 CodeBuddy 账号池：Token 有效期、签到与连通性（每 30 秒自动刷新）'
        }
        actions={
          <>
            {isAdmin && (
              <ConfirmDialog
                title="强制重启上游容器？"
                description="通常无需手动执行：添加或删除账号后会自动重载。仅当上游状态异常、需要强制重载时才使用。重启约 0.5 秒，在途请求会正常完成。"
                confirmText="重启"
                onConfirm={restartUpstream}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full" disabled={restarting}>
                    <Power className={restarting ? 'animate-spin' : ''} />
                    <span className="hidden sm:inline">强制重启</span>
                    <span className="sm:hidden">重启</span>
                  </Button>
                }
              />
            )}
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              onClick={refreshCredits}
              disabled={creditsBusy || !visible.length}
              title="直接向腾讯查询各账号当前积分（上游缓存的积分可能滞后数小时）"
            >
              <Coins className={creditsBusy ? 'animate-pulse' : ''} />
              <span className="hidden sm:inline">刷新积分</span>
              <span className="sm:hidden">积分</span>
            </Button>
            {/* 「全部签到」仅国内版显示：国际版**没有签到体系**（上游调度器对
                global 账号直接过滤，不发请求）。显示一个按下去只会得到「已跳过」
                的按钮是误导，直接不给。 */}
            {isAdmin && realm === 'cn' && (
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                onClick={checkinAll}
                disabled={checkinAllBusy || !visible.length}
              >
                <CalendarCheck className={checkinAllBusy ? 'animate-pulse' : ''} />
                全部签到
              </Button>
            )}
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                添加账号
              </Button>
            )}
          </>
        }
      />

      <section className="overflow-hidden rounded-[20px] bg-muted">
        {/* 手机端：卡片列表。表格 6 列在窄屏需要横向滚动，读一行要来回拖，
            改为纵向卡片后信息一眼可见 */}
        <div className="divide-y divide-border/40 md:hidden">
          {visible.map((a) => (
            <div key={a.file} className="space-y-2.5 px-3.5 py-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2.5">
                  {renderAvatar(a)}
                  <div className="min-w-0">
                    <div
                      className={
                        'truncate text-sm font-medium ' + (a.is_expired ? 'text-muted-foreground' : '')
                      }
                    >
                      {a.nickname || '未命名'}
                    </div>
                    <div className="truncate font-mono text-[10px] text-muted-foreground">{a.uid}</div>
                  </div>
                </div>
                {renderStatus(a)}
              </div>

              <div className="flex items-center justify-between gap-3">
                <div className="flex shrink-0 items-center gap-1.5">
                  <Coins className="h-3.5 w-3.5 text-muted-foreground" />
                  {renderCredits(a)}
                </div>
                {renderExpiry(a)}
              </div>

              {isAdmin && renderActions(a)}
            </div>
          ))}
          {!visible.length && !loading && (
            <div className="px-4 py-12 text-center text-xs text-muted-foreground">暂无账号</div>
          )}
          {loading && !merged.length && (
            <div className="px-4 py-12 text-center text-xs text-muted-foreground">加载中…</div>
          )}
        </div>

        {/* 桌面端：表格 */}
        <div className="hidden md:block">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">昵称</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">UID</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">状态</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">积分余额</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token 有效期</TableHead>
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">操作</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((a) => (
              <TableRow key={a.file} className="border-b border-border/40">
                <TableCell className="pl-4">
                  <div className="flex items-center gap-2.5">
                    {renderAvatar(a)}
                    <span className={'truncate text-sm font-medium ' + (a.is_expired ? 'text-muted-foreground' : '')}>
                      {a.nickname || '未命名'}
                    </span>
                    <Badge
                      variant="secondary"
                      className={
                        'shrink-0 rounded-md px-1.5 py-0 text-[10px] ' +
                        ((a.realm ?? 'cn') === 'global'
                          ? 'bg-sky-500/10 text-sky-700 dark:text-sky-400'
                          : '')
                      }
                    >
                      {realmLabel(a.realm)}
                    </Badge>
                  </div>
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{a.uid}</TableCell>
                <TableCell>{renderStatus(a)}</TableCell>
                <TableCell>{renderCredits(a)}</TableCell>
                <TableCell>{renderExpiry(a)}</TableCell>
                {isAdmin && <TableCell className="pr-4">{renderActions(a)}</TableCell>}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        </div>

        {!visible.length && !loading && (
          <EmptyState
            icon={Users}
            title="暂无账号"
            description={isAdmin ? '点击右上角「添加账号」扫码授权' : '请联系管理员添加账号'}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                添加账号
              </Button>
            )}
          </EmptyState>
        )}
        {loading && !merged.length && (
          <div className="py-16 text-center text-xs text-muted-foreground">加载中…</div>
        )}
      </section>

      {/* 签到与任务记录已独立成页（账号一多，堆在本页会越滑越长） */}
      <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-muted-foreground">
        <span>签到结果与上游自动任务记录已移至</span>
        <Link href="/tasks" className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 font-medium text-foreground transition-colors hover:bg-muted/70">
          任务记录
          <ChevronRight className="h-3 w-3" />
        </Link>
      </div>

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onSuccess={load} />
    </div>
  );
}
