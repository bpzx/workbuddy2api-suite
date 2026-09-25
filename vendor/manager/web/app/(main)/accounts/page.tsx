'use client';

import Link from 'next/link';
import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Gift,
  Zap,
  KeyRound,
  Pause,
  Play,
  Trash2,
  Plus,
  Users,
  Power,
  CalendarCheck,
  Check,
  Coins,
  ChevronRight,
  RotateCcw,
  Sparkles,
  StickyNote,
} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {accountApi, upstreamApi, errText} from '@/lib/api';
import type {Account, CreditExpiry, CreditsMeta, UpstreamStatus} from '@/lib/types';
import {expiryBarPercent, expiryVisual, fmtAgo, fmtDateTime, fmtNumber, fmtRemain} from '@/lib/format';
import {
  availabilityLabelKey,
  availabilityOf,
  isDegraded,
  mergePoolStatus,
  rateLimitedModels,
} from '@/lib/account-status';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {AddAccountDialog} from '@/components/common/accounts/AddAccountDialog';
import {CreditCountdown} from '@/components/common/accounts/CreditCountdown';
import {AccountNoteDialog} from '@/components/common/accounts/AccountNoteDialog';
import {AccountTaskDialog} from '@/components/common/accounts/AccountTaskDialog';
import {useAuth} from '@/lib/auth-context';
import {realmLabel, useRealm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
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
  const t = useT();
  const {isAdmin} = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [upstream, setUpstream] = useState<UpstreamStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  // 备注编辑（issue #67）：记的是**哪个账号**而不是布尔——弹窗要以该账号当前的
  // 备注为初值，否则会拿上一个账号的内容去保存。
  const [noteTarget, setNoteTarget] = useState<Account | null>(null);
  // 活动任务（单账号执行成长任务）的目标账号；null = 对话框关闭
  const [taskTarget, setTaskTarget] = useState<Account | null>(null);
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
        notify.ok(t('accounts.creditsRefreshed'), t('accounts.creditsRefreshedDetail', {ok: r.succeeded, total: r.total}));
      } else {
        notify.warn(t('accounts.creditsPartial'), t('accounts.creditsPartialDetail', {ok: r.succeeded, total: r.total}));
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
      // 两类账号都不计入分母，各自补一句说明，否则用户会以为漏签了：
      //   skipped — 国际版没有签到体系，点多少次都是「已跳过」
      //   already — 今天已经签过，本次没再打上游（这正是「不重复签到」的效果）
      const notes = [
        r.skipped > 0 ? t('accounts.checkinSkippedNote', {skipped: r.skipped}) : '',
        r.already > 0 ? t('accounts.checkinAlreadyNote', {n: r.already}) : '',
      ].filter(Boolean).join(' ');
      const note = notes ? ' ' + notes : '';
      if (r.total === 0) {
        // 「今天都已签到」和「没有可签到的账号」是两回事：前者是正常且理想的状态
        // （说明当天不用再操作），后者说明池子里没有国内版账号。混成一句「没有
        // 可签到的账号」会让前者看起来像出了问题。
        if (r.already > 0) {
          notify.ok(
            t('accounts.checkinNothingPending'),
            t('accounts.checkinNothingPendingDetail', {n: r.already}) + note,
          );
        } else {
          notify.info(t('accounts.noCheckinTargets'), notes.trim() || undefined);
        }
      } else if (failed === 0) {
        notify.ok(
          t('accounts.checkinAllDone'),
          t('accounts.checkinAllDoneDetail', {ok: r.succeeded, total: r.total}) + note,
        );
      } else {
        notify.warn(
          t('accounts.checkinPartial', {failed}),
          t('accounts.checkinPartialDetail', {ok: r.succeeded, total: r.total}) + note,
        );
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCheckinAllBusy(false);
    }
  }, [load]);

  /**
   * 将本地 auths 文件与上游账号池状态按 uid 合并。
   *
   * 合并与分档规则都在 `lib/account-status` 里——首页的健康快照要用**同一套**
   * 规则（此前它只看 Token 有效期，于是同一个账号首页说「在线」、这里说
   * 「未加载」，用户看到自相矛盾的面板）。
   */
  const merged = useMemo(() => mergePoolStatus(accounts, upstream), [accounts, upstream]);

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

  /**
   * 今天还没签到的账号数 —— 「全部签到」按钮据此显示与禁用。
   *
   * 判据与签到按钮的**可见条件**保持一致（国内版、且未被面板停用）：面板停用的
   * 账号已完全退出账号池、签到也跟着停了，把它算进「待签到」会让按钮牌子上
   * 永远留着一个点不掉、也签不了的数字。
   */
  const pendingCheckin = useMemo(
    () =>
      visible.filter(
        (a) =>
          (a.realm ?? 'cn') === 'cn' &&
          a.disabled_by_panel !== true &&
          a.checkin_today == null,
      ).length,
    [visible],
  );

  /** 执行单账号操作（签到 / 测活 / 刷新 / 删除），成功后同步底栏计数 */
  async function run(
    file: string,
    fn: () => Promise<unknown>,
    okMsg: string,
    preferOkMsg = false,
  ) {
    setBusyFile(file);
    try {
      const res = (await fn()) as {
        message?: string;
        ok?: boolean;
        credits?: number | null;
        expiries?: CreditExpiry[];
      };
      const ok = res.ok !== false;
      // 签到会返回刷新后的实时积分与到期时间，直接就地更新，省一次请求
      if (typeof res.credits === 'number') {
        setAccounts((prev) => prev.map((a) => (a.file === file ? {...a, credits: res.credits} : a)));
        const uid = accounts.find((a) => a.file === file)?.uid;
        if (uid && res.expiries) {
          setCreditsMeta((prev) => ({
            ...prev,
            [uid]: {...prev[uid], cached: false, cache_age: null, expiries: res.expiries},
          }));
        }
      }
      (ok ? notify.ok : notify.err)(ok && preferOkMsg ? okMsg : (res.message || okMsg));
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
      (res.ok ? notify.ok : notify.err)(res.message || t('accounts.restarted'));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRestarting(false);
    }
  }

  /** 账号状态徽章（表格与移动端卡片共用）。
   *
   *  分档顺序与文案都取自 `lib/account-status`——首页的健康快照用**同一套**
   *  判定，两页因此不会对同一个账号给出不同说法（此前首页只看 Token 有效期，
   *  于是同一个账号首页说「在线」、这里说「未加载」）。
   *  这里只负责「怎么画」，不再自行判断。 */
  function renderStatus(a: Account) {
    const tier = availabilityOf(a);
    const label = t(availabilityLabelKey(tier, a));

    if (tier === 'disabled') {
      const reason = String(a.disabled_reason || '');
      return (
        <Badge
          variant="destructive"
          className="rounded-full"
          title={reason ? t('accounts.disabledReason', {reason}) : undefined}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'disabledByPanel') {
      // 面板主动停用（改名）：中性灰而不是告警红——这是用户自己的选择，不是故障。
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeDisabledByPanelTitle')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'manualDisabled') {
      // 上游状态位停用（issue #45）：同样中性灰，但提示文案不同——这条是
      // 「还在池里、签到与保活照常」，与上一条「完全退出账号池」差别很大，
      // 用户看不出机制差别，必须写清楚。停用原因也一并带上。
      const reason = String(a.manual_reason || '');
      const nl = String.fromCharCode(10);
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeManualDisabledTitle') + (reason ? nl + reason : '')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'expired') {
      return <Badge variant="destructive" className="rounded-full">{label}</Badge>;
    }
    // 上游状态这次没取到 —— 冷却 / 禁用 / 在不在池里**全都无从判断**。
    // 本地能确定的（令牌是否过期）已在分档里先判过，不受影响。
    if (tier === 'unknown') {
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-muted-foreground"
          title={t('accounts.badgeUnknownTitle')}
        >
          {label}
        </Badge>
      );
    }
    if (tier === 'cooling') {
      // 带上「还要等多久」：只写「冷却中」的话用户不知道是几秒还是几小时，
      // 只能反复刷新碰运气。剩余时间是上游状态机给的权威值。
      const secs = a.cool_remaining_sec;
      const left = typeof secs === 'number' && secs > 0 ? fmtRemain(secs) : '';
      // 按**原因**分组展示：上游两类条目的含义完全不同，混在一起会误导——
      //   6004  → 这个模型被限流了（等一会儿就好）
      //   11102 → 这个账号根本没有这个模型（等多久都不会好，该换模型）
      // 上游的 reason 前缀就是判据（"6004 model rate limit" / "11102 model not available"）。
      const limited: string[] = [];
      const missing: string[] = [];
      // 枚举分隔符跟随语言：中文用「、」，拉丁语系用「, 」
      const sep = t('common.listSeparator');
      for (const m of a.rate_limited_models ?? []) {
        const r = m.reason ?? '';
        (r.startsWith('11102') ? missing : limited).push(m.model);
      }
      const tip = [
        // 降权要**排在剩余时间前面**：它是「为什么在冷却」的答案，而剩余时间
        // 只是「还要等多久」。先给原因，用户才知道该等还是该去查这个号。
        isDegraded(a)
          ? t('accounts.degradedReason', {n: a.consecutive_fails ?? 0})
          : '',
        left ? t('accounts.etaRecovery', {left}) : '',
        limited.length ? t('accounts.limitedModels', {models: limited.join(sep)}) : '',
        missing.length
          ? t('accounts.missingModels', {models: missing.join(sep)})
          : '',
      ]
        .filter(Boolean)
        .join('\n');
      const total = limited.length + missing.length;
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-amber-600 dark:text-amber-400"
          title={tip || undefined}
        >
          {label}{left ? ` · ${left}` : ''}
          {total > 0 && <span className="ml-1 opacity-70">{t('accounts.modelsCount', {count: total, n: total})}</span>}
        </Badge>
      );
    }
    // 「一直在失败，但状态看着正常」——上游对**未命中它那几条规则**的 4xx
    // （例如被 WAF 拦下的 403）只「换号不罚」：不冷却、不熔断、不禁用
    // （其 applyErrorPolicy 的 default 分支，为防雪崩而刻意如此）。于是这种
    // 账号在面板上一直显示「正常」，实际每次请求都失败，可持续几小时
    // （issue #14 报告的第二点）。我们能做的是**把它标出来** —— 否则用户
    // 只看到「状态正常」却一直在报错，完全无从下手。
    if (tier === 'neverSucceeded') {
      const errs = typeof a.err_total === 'number' ? a.err_total : 0;
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-rose-600 dark:text-rose-400"
          title={t('accounts.badgeNeverSucceededTitle', {errs})}
        >
          {label}
        </Badge>
      );
    }
    // 上游没有加载这个账号 —— 它**不在账号池里，永远选不中**。
    //
    // 为什么必须单独标：面板读的是 auths/ 目录下的文件，上游读的才是池。
    // 上游 `LoadDir` 对解析失败的 auth 文件静默跳过（如 accessToken 为空），
    // 那种文件永远进不了池。此前我们会兜底显示「● 在线」——于是出现
    // 「面板全绿、调用却报没有健康账号」的矛盾，用户完全无从下手（实测反馈）。
    //
    // 也可能只是「刚添加、上游还没重载」，所以文案不写成故障，
    // 而是说明它尚未进入账号池、并给出可做的动作。
    if (tier === 'notLoaded') {
      const why = String(a.invalid_reason || '');
      return (
        <Badge
          variant="secondary"
          className="rounded-full text-rose-600 dark:text-rose-400"
          title={why ? t('accounts.badgeNotLoadedWhy', {why}) : t('accounts.badgeNotLoadedTitle')}
        >
          {label}
        </Badge>
      );
    }
    return (
      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
        {label}
      </Badge>
    );
  }

  /** 模型级限流标记（issue #43）。
   *
   *  为什么单独做一个徽章、而不是并进状态列：**账号本身是在线的**，只是某个
   *  模型暂时被腾讯限流（6004）。用户的实际观察是「换个模型就能继续用」，
   *  但状态列显示「在线」，于是「这个模型用不了」这件事在面板上完全不可见
   *  —— 有人只能进容器翻 state.json 才知道。
   *
   *  所以它与状态徽章**并列**展示（不是替代）：账号确实可用，只是有模型受限。
   *  徽章里列具体有哪些模型受限，悬停再给出每个模型的恢复时间——上游台账里
   *  带权威的重置时刻。 */
  function renderModelLimit(a: Account) {
    const limited = rateLimitedModels(a);
    if (!limited.length) return null;
    const sep = t('common.listSeparator');
    const lines = limited.map((m) => {
      const until = a.rate_limited_models?.find((x) => x.model === m.model)?.until;
      const at = until ? new Date(until).toLocaleTimeString() : '';
      // 11102（该账号没有这个模型）与 6004（限流）含义不同：前者等多久都不会好，
      // 该换模型；后者等一会儿就恢复。上游的 reason 前缀就是判据。
      const why = m.reason.startsWith('11102')
        ? t('accounts.modelMissing')
        : at
          ? t('accounts.modelLimitUntil', {at})
          : t('accounts.modelLimited');
      return `${m.model} · ${why}`;
    });
    // 徽章里**直接给出模型名**（用户反馈：只知道「有 2 个模型受限」不够用，得知道
    // 是哪些，才能换模型或者告诉调用方避开它们）。名字可能很长（`global:` 前缀的
    // 型号尤甚），所以最多列两个，其余用「+N」带过；完整清单与各自恢复时间仍在
    // 悬停里——那里才是逐条说明的地方。
    const MAX_INLINE = 2;
    const names = limited.slice(0, MAX_INLINE).map((m) => m.model).join(sep);
    const shown = limited.length > MAX_INLINE
      ? `${names} +${limited.length - MAX_INLINE}`
      : names;
    return (
      <Badge
        variant="secondary"
        className="rounded-full text-amber-600 dark:text-amber-400"
        title={[t('accounts.modelLimitTitle'), ...lines].join(String.fromCharCode(10))}
      >
        {t('accounts.modelLimitBadge', {models: shown})}
      </Badge>
    );
  }

  /** 积分余额 + 数据来源标注。
   *  明确区分「实时」「缓存 x 秒前」与「上游快照」三种来源，避免把滞后的数字
   *  当成刚查到的。
   *
   *  为什么要单独标出「上游快照」（issue #56）：实时值取不到时（查询失败，或
   *  只读账号早先根本调不到该接口），界面会回退到上游 `/status` 的快照值 ——
   *  那是**上游上次调度这个账号时记下的**，可能滞后数小时，也可能仍是 0。
   *  实测只读账号看到两个账号显示 0（管理员视角是 6420 / 9353），而 0 会被当成
   *  「余额耗尽」用红色渲染，看起来像账号出了故障。
   *
   *  所以快照值：① 打「上游快照」标签；② **不用红色** ——「快照说 0」不等于
   *  「确实没积分」，不该报警。
   */
  function renderCredits(a: Account) {
    // 优先用刚查到的实时值，其次上游 /status 的快照值
    const live = liveCredits[a.uid];
    const value = live ?? a.credits;
    if (value === null || value === undefined) {
      return (
        <span
          className="text-xs text-muted-foreground"
          title={t('accounts.creditsNoneTitle')}
        >
          —
        </span>
      );
    }
    const meta = creditsMeta[a.uid];
    const fromSnapshot = live === undefined;
    const tone = fromSnapshot
      ? 'text-muted-foreground'
      : value <= 0
        ? 'text-red-600 dark:text-red-400'
        : value < 200
          ? 'text-amber-600 dark:text-amber-400'
          : 'text-foreground';
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className={`text-xs font-medium tabular-nums ${tone}`} title={t('accounts.creditsTitle')}>
          {fmtNumber(value)}
        </span>
        {fromSnapshot ? (
          <span
            className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] leading-3 text-muted-foreground"
            title={t('accounts.snapshotTitle')}
          >
            {t('accounts.snapshot')}
          </span>
        ) : (
          meta &&
          (meta.cached ? (
            <span
              className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-amber-600 dark:text-amber-400"
              title={t('accounts.cacheTitle')}
            >
              {meta.cache_age != null ? t('accounts.cacheAge', {n: meta.cache_age}) : t('accounts.cache')}
            </span>
          ) : (
            <span
              className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] leading-3 text-emerald-600 dark:text-emerald-400"
              title={t('accounts.liveTitle')}
            >
              {t('accounts.live')}
            </span>
          ))
        )}
        <CreditCountdown expiries={fromSnapshot ? undefined : meta?.expiries} />
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
            title={t('accounts.issuedAt', {at: fmtDateTime(issued)})}
          >
            {t('accounts.lastRenewed', {ago: fmtAgo(issued)})}
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
    // 今天已经签过：按钮转为「已签到」并禁用。
    // 为什么要在界面上拦：腾讯对重复签到回 10001（幂等成功），所以点下去「看着
    // 也能成功」——但每次都真打一次 RPC，还会在签到记录里堆一串「今日已签到」，
    // 把真正的失败记录挤下去。后端另有一道拦截（不发请求），这里是让用户
    // 一眼看出「不用再点了」，而不是点了之后才知道。
    const checkinDone = a.checkin_today != null;
    // 本面板改名停用（issue #21）：账号已完全退出账号池，签到等任务也随之停掉，
    // 所以隐藏其它操作（它们对这个号已无意义）。
    const paused = a.disabled_by_panel === true;
    // 上游状态位停用（issue #45）：**任务照常执行**是这条路的重点，所以签到 /
    // 测试 / 刷新 Token 这些操作仍然可用、也必须仍然可见——否则用户会以为
    // 「停用把签到也停了」，正好把这条路与改名那条的区别抹掉了。
    const viaBit = a.manual_disabled === true;
    const off = paused || viaBit;
    const hasClearableState = a.cooling === true || rateLimitedModels(a).length > 0;
    return (
      <div className="flex justify-end gap-1">
        {/* 备注（issue #67）：只动本端库里的一行文本，不碰上游、不重启容器，
            所以放在最前——它是最轻的动作。任何状态下的账号都能写备注：
            停用的号恰恰更容易忘了它是谁。 */}
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 rounded-md"
          title={a.note ? t('accounts.noteEditTitle') : t('accounts.noteAdd')}
          disabled={busy}
          onClick={() => setNoteTarget(a)}
        >
          <StickyNote className={'h-3.5 w-3.5 ' + (a.note ? 'text-amber-600 dark:text-amber-400' : '')} />
        </Button>
        {/* 临时停用 / 启用（issue #21、#45）。放在最前：它是最轻的「止血」动作——
            某个号在拖后腿（一直失败、触发风控）时，先停用它比删掉更合适
            （删除会丢凭证、只能重新扫码；停用是可逆的）。 */}
        {(!off || viaBit) && canCheckin && (
          <Button
            variant="ghost"
            size="icon"
            className={
              'h-7 w-7 rounded-md ' +
              (checkinDone
                ? 'text-emerald-600 hover:text-emerald-600 disabled:opacity-100 dark:text-emerald-400'
                : '')
            }
            title={
              checkinDone
                ? t('accounts.checkinDoneTitle', {at: fmtDateTime(a.checkin_today as number)})
                : t('accounts.checkin')
            }
            disabled={busy || checkinDone}
            onClick={() => run(a.file, () => accountApi.checkin(a.file), t('accounts.opDone'))}
          >
            {checkinDone ? <Check className="h-3.5 w-3.5" /> : <Gift className="h-3.5 w-3.5" />}
          </Button>
        )}
        {/* 活动任务（单账号）：与「任务」页的一键执行是同一套（调用上游
            task_runner.py），区别是只对这一个账号跑 —— 池子里某个号想单独
            补一轮任务时，不必把全部账号再跑一遍。国际版不适用 CN 任务中心
            （脚本自己会 skip），所以与签到按钮同一个可见条件。 */}
        {(!off || viaBit) && canCheckin && (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 rounded-md"
            title={t('accounts.taskRun')}
            disabled={busy}
            onClick={() => setTaskTarget(a)}
          >
            <Sparkles className="h-3.5 w-3.5" />
          </Button>
        )}
        {(!off || viaBit) && (
          <>
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('accounts.test')} disabled={busy}
              onClick={() => run(a.file, () => accountApi.test(a.file), t('accounts.testDone'))}>
              <Zap className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('accounts.refreshToken')} disabled={busy}
              onClick={() => run(a.file, () => accountApi.refresh(a.file), t('accounts.refreshDone'))}>
              <KeyRound className="h-3.5 w-3.5" />
            </Button>
          </>
        )}
        {!paused && hasClearableState && (
          <ConfirmDialog
            title={t('accounts.forceClearCoolingTitle')}
            description={t('accounts.forceClearCoolingDesc')}
            confirmText={t('accounts.forceClearCoolingConfirm')}
            destructive
            onConfirm={() => run(
              a.file,
              () => accountApi.clearCooling(a.file),
              t('accounts.forceClearCoolingDone'),
              true,
            )}
            trigger={
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 rounded-md text-rose-600 hover:text-rose-700 dark:text-rose-400"
                title={t('accounts.forceClearCooling')}
                disabled={busy}
              >
                <RotateCcw className="h-3.5 w-3.5" />
              </Button>
            }
          />
        )}
        <Button
          variant="ghost"
          size="icon"
          className={
            'h-7 w-7 rounded-md ' +
            (off ? 'text-emerald-600 hover:text-emerald-600' : 'text-amber-600 hover:text-amber-600')
          }
          title={off ? t('accounts.enableTitle') : t('accounts.disableTitle')}
          disabled={busy}
          onClick={() => run(
            a.file,
            () => accountApi.setDisabled(a.file, !off),
            // 文案如实反映用的是哪种机制：状态位停用后任务照常，改名停用则全停。
            off ? t('accounts.enabled')
                : (viaBit ? t('accounts.manualDisabled') : t('accounts.disabled')),
          )}
        >
          {off ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        </Button>
        <ConfirmDialog
          title={t('accounts.deleteTitle', {name: a.nickname || a.uid})}
          description={t('accounts.deleteDesc')}
          confirmText={t('accounts.delete')}
          destructive
          onConfirm={() => run(a.file, () => accountApi.remove(a.file), t('accounts.deleted'))}
          trigger={
            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title={t('accounts.delete')}>
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
        title={t('accounts.title')}
        description={
          realm === 'global'
            ? t('accounts.descGlobal')
            : t('accounts.descCn')
        }
        actions={
          <>
            {isAdmin && (
              <ConfirmDialog
                title={t('accounts.restartTitle')}
                description={t('accounts.restartDesc')}
                confirmText={t('accounts.restartConfirm')}
                onConfirm={restartUpstream}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full" disabled={restarting}>
                    <Power className={restarting ? 'animate-spin' : ''} />
                    <span className="hidden sm:inline">{t('accounts.forceRestart')}</span>
                    <span className="sm:hidden">{t('accounts.restartShort')}</span>
                  </Button>
                }
              />
            )}
            {/* 「刷新积分」= 强制查询（绕过 60 秒缓存），只有管理员能调。
                只读账号页面加载时本来就会走缓存路径拿到实时值，所以这个按钮
                对它们没有意义 —— 显示出来只会点了报 403（issue #56 之后权限
                是明确的：force 仍限管理员）。与上面几个管理员操作同一个口径。 */}
            {isAdmin && (
            <Button
              size="sm"
              variant="outline"
              className="rounded-full"
              onClick={refreshCredits}
              disabled={creditsBusy || !visible.length}
              title={t('accounts.refreshCreditsTitle')}
            >
              <Coins className={creditsBusy ? 'animate-pulse' : ''} />
              <span className="hidden sm:inline">{t('accounts.refreshCredits')}</span>
              <span className="sm:hidden">{t('accounts.creditsShort')}</span>
            </Button>
            )}
            {/* 「全部签到」仅国内版显示：国际版**没有签到体系**（上游调度器对
                global 账号直接过滤，不发请求）。显示一个按下去只会得到「已跳过」
                的按钮是误导，直接不给。 */}
            {/* 「全部签到」反映当天状态：
                · 还有待签的 → 文案回到「全部签到」，并带上待签数（不用点进去数）
                · 今天都签过了 → 文案变「今日已签」并禁用。禁用比「点了提示已签到」
                  更好：重复点击本来就不该发生，让按钮自己说出原因，比点完再报错省一次往返。
                判据 pendingCheckin 与每行签到按钮的可见条件一致，不会出现
                「按钮说还有 1 个、列表里却找不到那个号」的矛盾。 */}
            {isAdmin && realm === 'cn' && (
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                onClick={checkinAll}
                disabled={checkinAllBusy || pendingCheckin === 0}
                title={
                  pendingCheckin === 0
                    ? t('accounts.checkinAllNoneTitle')
                    : t('accounts.checkinAllTitle', {n: pendingCheckin})
                }
              >
                <CalendarCheck className={checkinAllBusy ? 'animate-pulse' : ''} />
                {pendingCheckin === 0 ? t('accounts.checkinAllNone') : t('accounts.checkinAll')}
                {pendingCheckin > 0 && (
                  <span className="tabular-nums opacity-70">{pendingCheckin}</span>
                )}
              </Button>
            )}
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                {t('accounts.addAccount')}
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
                      {a.nickname || t('accounts.unnamed')}
                    </div>
                    <div className="truncate font-mono text-[10px] text-muted-foreground">{a.uid}</div>
                    {a.note ? (
                      <div className="truncate text-[10px] text-muted-foreground" title={a.note}>
                        {t('accounts.noteLabel')}{a.note}
                      </div>
                    ) : null}
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-1.5">
                  {renderStatus(a)}
                  {renderModelLimit(a)}
                </div>
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
            <div className="px-4 py-12 text-center text-xs text-muted-foreground">{t('accounts.tableEmpty')}</div>
          )}
          {loading && !merged.length && (
            <div className="px-4 py-12 text-center text-xs text-muted-foreground">{t('common.loading')}</div>
          )}
        </div>

        {/* 桌面端：表格 */}
        <div className="hidden md:block">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('accounts.colNickname')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">UID</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('metric.credits')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colExpiry')}</TableHead>
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((a) => (
              <TableRow key={a.file} className="border-b border-border/40">
                <TableCell className="pl-4">
                  <div className="flex items-center gap-2.5">
                    {renderAvatar(a)}
                    <div className="min-w-0">
                      <div className={'truncate text-sm font-medium ' + (a.is_expired ? 'text-muted-foreground' : '')}>
                        {a.nickname || t('accounts.unnamed')}
                      </div>
                      {a.note ? (
                        <div className="truncate text-[10px] text-muted-foreground" title={a.note}>
                          {t('accounts.noteLabel')}{a.note}
                        </div>
                      ) : null}
                    </div>
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
                <TableCell>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {renderStatus(a)}
                    {renderModelLimit(a)}
                  </div>
                </TableCell>
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
            title={t('accounts.emptyTitle')}
            description={isAdmin ? t('accounts.emptyDescAdmin') : t('accounts.emptyDescViewer')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={() => setAddOpen(true)}>
                <Plus />
                {t('accounts.addAccount')}
              </Button>
            )}
          </EmptyState>
        )}
        {loading && !merged.length && (
          <div className="py-16 text-center text-xs text-muted-foreground">{t('common.loading')}</div>
        )}
      </section>

      {/* 签到与任务记录已独立成页（账号一多，堆在本页会越滑越长） */}
      <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-muted-foreground">
        <span>{t('accounts.movedTo')}</span>
        <Link href="/tasks" className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 font-medium text-foreground transition-colors hover:bg-muted/70">
          {t('nav.tasks')}
          <ChevronRight className="h-3 w-3" />
        </Link>
      </div>

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onSuccess={load} />
      <AccountNoteDialog
        account={noteTarget}
        open={noteTarget !== null}
        onOpenChange={(open) => { if (!open) setNoteTarget(null); }}
        onSaved={load}
      />
      <AccountTaskDialog
        account={taskTarget}
        open={taskTarget !== null}
        onOpenChange={(open) => { if (!open) setTaskTarget(null); }}
        onFinished={load}
      />
    </div>
  );
}
