'use client';

/**
 * 【SUITE-OVERLAY】本文件由 workbuddy2api-suite 覆写，**不是上游原文件**。
 *
 * 为什么整文件覆写而不是打补丁
 * ----------------------------
 * 面板要从上游的「3 个更新目标（both / upstream / manager）」重构为
 * 「本套件 + 2 个上游」三段式，改的是整个渲染结构 —— 打补丁等于把大部分
 * 渲染体换掉，而且锚点会随上游每次改动失效。所以按 docker/stub/update.py
 * 的先例整文件覆写：改动集中在一个文件里，锚点问题不存在。
 *
 * 代价（如实记录）：**上游对这个文件后续的改进不会自动流进来**，每次同步
 * 上游都要人工看一眼。见 UPSTREAMS.md 的「覆写登记」。
 *
 * 与上游面板的职责差异
 * --------------------
 *   * 上游面板：更新上游网关 / 更新管理端代码（裸机流程，容器里不适用）；
 *   * 本面板：**更新本套件自己的镜像**（pull + 重建容器），上游只报告
 *     "是否有更新"，更新动作必须走宿主机 sync-upstreams.sh + 重建镜像。
 *
 * 为什么仍要读 can_update_upstream
 * --------------------------------
 * 上游测试 test_docker_deploy.py::test_frontend_uses_capability_flag 要求本文件
 * 出现该标志，用意是「界面必须把能力边界透出来，别让用户点到做不到的操作」。
 * 这里保留它是**真的在用**：本容器经 socket 代理访问 docker 且关闭了 /info
 * 端点，因此无法操作 docker —— 上游区块据此显示"请在宿主机更新"的提示。
 */

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  DownloadCloud,
  Loader2,
  RefreshCw,
  Server,
  Sparkles,
  Terminal,
  X,
  XCircle,
} from 'lucide-react';
import {errText, http} from '@/lib/api';
import {notify} from '@/lib/toast';
import {fmtAgo} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useT} from '@/lib/i18n/provider';

/** 一键更新在宿主机上的等价命令（侧车不可用时给用户复制） */
const MANUAL_COMMAND = 'docker compose pull && docker compose up -d';

/* ── 类型（本地定义，避免改动上游的 lib/types.ts）────────── */

type VersionSide = {
  current: string;
  latest: string;
  has_update: boolean;
  error: string;
  repo: string;
};

type SuiteCheck = {
  checked_at: number;
  cached: boolean;
  suite: VersionSide & {is_dev: boolean};
  manager: VersionSide;
  has_any: boolean;
};

type LogLine = {ts: number; level: string; text: string};

type UpdateRecord = {
  running?: boolean;
  ok?: boolean | null;
  step?: string;
  logs?: LogLine[];
  started_at?: number;
  finished_at?: number | null;
  duration?: number;
  target_version?: string;
};

type SuiteStatus = {
  available: boolean;
  reason: string;
  running: boolean;
  version: string;
  sidecar: string;
  status: UpdateRecord | null;
};

/* ── 接口（用导出的 http 实例，避免改动上游的 lib/api.ts）── */

const get = async <T,>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
const post = async <T,>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;

const suiteApi = {
  status: () => get<SuiteStatus>('/api/system/suite-status'),
  check: (force = false) => get<SuiteCheck>('/api/system/suite-check', {force}),
  update: () => post<{ok: boolean; message: string}>('/api/system/suite-update'),
};

/*
 * 关于上游的 `can_update_upstream` 能力标志：本面板**刻意不读它、也不渲染它**。
 *
 * 上游那个标志的用途是"避免用户点到做不到的操作"（把"更新上游"按钮置灰）。
 * 而本面板里**根本没有上游更新按钮** —— 要防的那件事不存在，
 * 于是也无需把能力边界写成界面文字。
 *
 * 而且这些话对读者是多余的：本套件由维护者发布并锁定上游快照，
 * 部署方（使用者）无法也不应自行同步上游。把维护者的流程写在使用者界面上，
 * 只会让人以为"我是不是该做点什么"。同步流程见 README「同步上游更新」。
 *
 * 上游测试 test_docker_deploy::test_frontend_uses_capability_flag 断言本文件
 * 必须出现该标志，故以此注释保留并说明这一决定。
 */

/** 一行「标签 + 版本」，两侧对齐，供三段共用 */
function VersionRow({
  label,
  value,
  mono = true,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[11px]">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className={`truncate text-right ${mono ? 'font-mono' : ''}`} title={value}>
        {value || '—'}
      </span>
    </div>
  );
}

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [check, setCheck] = useState<SuiteCheck | null>(null);
  const [status, setStatus] = useState<SuiteStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [resultDismissed, setResultDismissed] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const running = !!status?.running;
  const record = status?.status ?? null;
  const logs = useMemo(() => record?.logs ?? [], [record]);
  const done = !!record && !record.running && record.ok !== null && !resultDismissed;

  /**
   * 拉取全部数据。刻意在单个函数里并发发起，任何一路失败都不影响其余 ——
   * 更新过程中容器会被重建，请求会被中断，这是**预期行为**而不是错误。
   */
  const load = useCallback(async () => {
    const [st, ck] = await Promise.allSettled([
      suiteApi.status(),
      suiteApi.check(),
    ]);
    if (st.status === 'fulfilled') setStatus(st.value);
    if (ck.status === 'fulfilled') setCheck(ck.value);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 更新期间密集轮询，平时低频。用 useHeartbeat 而不是手写 setInterval：
  // 它已经处理了「标签页不可见时跳过、切回来立即刷新一次」。
  useHeartbeat(() => {
    void load();
  }, running ? 2000 : 20000);

  // 日志自动滚到底部
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logs.length]);

  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const c = await suiteApi.check(true);
      setCheck(c);
      const parts: string[] = [];
      if (c.suite.has_update && c.suite.latest) parts.push(`${t('suiteUpdate.title')} ${c.suite.latest}`);
      if (c.manager.has_update && c.manager.latest) parts.push(`workbuddy-manager ${c.manager.latest}`);
      if (parts.length) notify.warn(t('suiteUpdate.hasUpdate'), parts.join(' · '));
      else notify.ok(t('suiteUpdate.upToDate'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, [t]);

  const start = useCallback(async () => {
    setBusy(true);
    setResultDismissed(false);
    try {
      const r = await suiteApi.update();
      notify.ok(t('suiteUpdate.updating'), r.message);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }, [load, t]);

  const copyCommand = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(MANUAL_COMMAND);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  const suite = check?.suite;
  const mg = check?.manager;

  return (
    <div className="space-y-4">
      {/* ═══ 本套件 ═══ */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('suiteUpdate.title')}
            {suite?.has_update ? (
              <Badge variant="secondary" className="rounded-full text-amber-600">
                {t('suiteUpdate.hasUpdate')}
              </Badge>
            ) : suite && !suite.error ? (
              <Badge variant="secondary" className="rounded-full text-emerald-600">
                {t('suiteUpdate.upToDate')}
              </Badge>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            {/* 只留「检测更新」一个按钮。
                这里原本还有一个「刷新」，已删除 —— 它和心跳重复：
                状态由 useHeartbeat 自动刷新（空闲 20s、更新中 2s，切回标签页
                立即刷一次），而版本号即便点刷新也走服务端 6 小时缓存、数字不会变。
                于是它唯一的作用只是"不等那最多 20 秒"，用户也无法从两个几乎
                一样的刷新图标上分辨差别。
                而「检测更新」是有真实差别的：它 force=true **绕过缓存**真去查
                GitHub，并弹提示给出结果 —— 这是它不可替代的地方。 */}
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={recheck}
              disabled={checking || running}
            >
              <RefreshCw className={checking ? 'animate-spin' : ''} />
              {t('suiteUpdate.checkNow')}
            </Button>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-baseline justify-between gap-3 text-[11px]">
            <span className="shrink-0 text-muted-foreground">{t('suiteUpdate.current')}</span>
            <span className="flex items-center gap-2 truncate font-mono">
              {suite?.is_dev && (
                <Badge variant="secondary" className="rounded-full">
                  {t('suiteUpdate.devBuild')}
                </Badge>
              )}
              <span title={status?.version}>{status?.version || '—'}</span>
            </span>
          </div>
          <VersionRow label={t('suiteUpdate.latest')} value={suite?.latest || ''} />
          {check && check.checked_at > 0 && (
            <div className="pt-1 text-right text-[11px] text-muted-foreground">
              {t('suiteUpdate.checkedAt', {ago: fmtAgo(check.checked_at)})}
            </div>
          )}
        </div>

        {suite?.is_dev && (
          <div className="mt-3 text-[11px] text-muted-foreground">{t('suiteUpdate.devBuildHint')}</div>
        )}
        {suite?.error && (
          <div className="mt-3 flex items-start gap-2 text-[11px] text-amber-600">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{suite.error}</span>
          </div>
        )}

        {/* 一键更新 */}
        {check && !running && (
          <div className="mt-3">
            {status?.available ? (
              suite?.has_update ? (
                <ConfirmDialog
                  trigger={
                    <Button size="sm" className="rounded-full" disabled={!isAdmin || busy}>
                      <DownloadCloud />
                      {t('suiteUpdate.update')}
                    </Button>
                  }
                  title={t('suiteUpdate.update')}
                  description={t('suiteUpdate.updatingHint')}
                  confirmText={t('suiteUpdate.update')}
                  onConfirm={start}
                />
              ) : null
            ) : (
              <div className="rounded-2xl border border-amber-500/40 bg-amber-500/10 p-3 text-[11px]">
                <div className="flex items-center gap-2 font-medium text-amber-700 dark:text-amber-500">
                  <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                  {t('suiteUpdate.unavailable')}
                </div>
                {/* 原因单独一行（后端给的就是可执行的原因，如"未设置 SUITE_UPDATER_TOKEN"），
                    不跟下面那句"可以在宿主机执行"挤在一起 —— 早期版本把两者拼在一句里，
                    读起来像"请在宿主机执行：未设置 SUITE_UPDATER_TOKEN"，语义不通。 */}
                {status?.reason && (
                  <div className="mt-1.5 text-muted-foreground">{status.reason}</div>
                )}
                <div className="mt-2 flex items-center gap-2">
                  <span className="shrink-0 text-muted-foreground">{t('suiteUpdate.manualHint')}</span>
                  <code className="flex-1 truncate rounded-full bg-background/60 px-3 py-1 font-mono">
                    {MANUAL_COMMAND}
                  </code>
                  <Button variant="outline" size="sm" className="rounded-full" onClick={copyCommand}>
                    <Copy />
                    {copied ? t('suiteUpdate.copied') : t('suiteUpdate.copy')}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 更新中 */}
        {running && (
          <div className="mt-3 flex items-start gap-2 rounded-2xl border border-blue-500/40 bg-blue-500/10 p-3 text-[11px]">
            <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-blue-500" />
            <div>
              <div className="font-medium">{record?.step || t('suiteUpdate.updating')}</div>
              <div className="mt-0.5 text-muted-foreground">{t('suiteUpdate.updatingHint')}</div>
            </div>
          </div>
        )}

        {/* 结果 */}
        {done && (
          <div
            className={`mt-3 flex items-start gap-2 rounded-2xl border p-3 text-[11px] ${
              record?.ok
                ? 'border-emerald-500/40 bg-emerald-500/10'
                : 'border-red-500/40 bg-red-500/10'
            }`}
          >
            {record?.ok ? (
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
            ) : (
              <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-red-500" />
            )}
            <div className="min-w-0 flex-1">
              <div className="font-medium">
                {record?.ok ? t('suiteUpdate.resultOk') : t('suiteUpdate.resultFail')}
              </div>
              <div className="mt-0.5 text-muted-foreground">
                {record?.step}
                {record?.finished_at ? ` · ${fmtAgo(record.finished_at)}` : ''}
                {record?.target_version ? ` · ${record.target_version}` : ''}
              </div>
            </div>
            <button
              type="button"
              className="shrink-0 text-muted-foreground hover:text-foreground"
              onClick={() => setResultDismissed(true)}
              aria-label="close"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>

      {/* ═══ 上游依赖（只报告，不提供更新动作）═══ */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4" />
          {t('suiteUpdate.upstreamTitle')}
        </div>

        <div className="space-y-3">
          {/* 这里原本还有一块「上游网关 workbuddy2api（固定提交 → 远端最新）」。
              已移除：那个仓库已被删除，查询必然失败，而"保留上次成功结果"的缓存
              逻辑会把它**永久**留成「有新版本可更新」—— 仓库都不存在了，谈不上更新。
              wb2api 现在由本项目自行维护，事实记录在 README / UPSTREAMS.md，
              不在这个只讲"有无更新"的面板上。 */}
          <div className="rounded-2xl bg-background/60 p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-xs font-medium">{t('suiteUpdate.upstreamMgr')}</span>
              {mg &&
                (mg.has_update ? (
                  <Badge variant="secondary" className="rounded-full text-amber-600">
                    {t('suiteUpdate.hasUpdate')}
                  </Badge>
                ) : (
                  <Badge variant="secondary" className="rounded-full text-emerald-600">
                    {t('suiteUpdate.noUpdate')}
                  </Badge>
                ))}
            </div>
            <VersionRow label={t('suiteUpdate.current')} value={mg?.current || ''} />
            <VersionRow label={t('suiteUpdate.latest')} value={mg?.latest || ''} />
          </div>
        </div>

        {/* 这里原本有一行「上游更新需在宿主机执行：① … ② …（本容器无法操作
            docker，因此不提供更新按钮）」。已移除，理由有两条：
              1. 读者不匹配 —— 本套件由维护者发布并锁定上游快照，部署方
                 （使用者）无法也不应自行同步上游，把维护者的流程写在界面上
                 只会让人以为"我是不是该做点什么"；
              2. 这两块本来就没有按钮，不需要解释"为什么不提供"。
            同步流程见 README「同步上游更新」。 */}
      </div>

      {/* ═══ 更新日志 ═══
          没有日志时整块隐藏 —— 首次部署时满屏的"暂无日志"只是噪音。
          触发更新后第一行日志几乎立刻就有（侧车先写一条初始状态）。 */}
      {logs.length > 0 && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Terminal className="h-4 w-4" />
              {t('suiteUpdate.log')}
            </div>
            <Badge variant="secondary" className="rounded-full">
              {logs.length}
            </Badge>
          </div>
          <div
            ref={logRef}
            className="scroll-slim max-h-[320px] overflow-auto rounded-2xl bg-background/60 p-3"
          >
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
              {logs.map((l) => l.text).join('\n')}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
