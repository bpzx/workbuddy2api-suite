'use client';

import {useCallback, useEffect, useState} from 'react';
import {ShieldCheck, Plus, Trash2, Ban, CircleCheck, Network} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {securityApi, errText} from '@/lib/api';
import type {AuditLog, IpAccessLog, IpRule, SecurityConfig} from '@/lib/types';
import {fmtDateTime} from '@/lib/format';
import {FileClock} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {settingsApi} from '@/lib/api';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
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

/** 审计动作 → i18n 键（动作本身是后端固定的英文枚举，这里只做展示名映射） */
const AUDIT_LABEL_KEYS: Record<string, string> = {
  login: 'security.auditLogin',
  login_failed: 'security.auditLoginFailed',
  update_user: 'security.auditUpdateUser',
  delete_user: 'security.auditDeleteUser',
  add_user: 'security.auditAddUser',
};

/**
 * 审计详情本地化。
 *
 * 后端把详情存成中文（如「角色=admin；来源 1.2.3.4」）——存储层保持语言中立
 * （否则同一条记录会因写入者语言不同而不一致），展示时按固定分词翻译已知标记，
 * 认不出的片段原样保留。
 */
function auditDetail(detail: string | null | undefined, t: (key: string, params?: Record<string, string>) => string): string {
  if (!detail) return '—';
  return detail
    .split('；')
    .map((part) => {
      const from = /^来源\s+(.+)$/.exec(part);
      if (from) return t('security.detailFrom', {ip: from[1]});
      if (part === '密码=已重置') return t('security.detailPasswordReset');
      const role = /^角色=(.+)$/.exec(part);
      if (role) return t('security.detailRole', {role: role[1]});
      // 「段=upstream,pool」：本次实际改动过的配置段。段名是后端的键名，不翻译，
      // 只把「段=」这个标签换掉。
      const segment = /^段=(.*)$/.exec(part);
      if (segment) return t('security.detailSegment', {segments: segment[1]});
      // 「改密码、改角色」：用户资料变更标记，用「、」连接成一串，逐个词翻译后
      // 再用目标语言的连接符拼回去。认不全就整段原样保留（宁可不译也不译错）。
      const tokens = part.split('、').map((token) => {
        if (token === '改密码') return t('security.detailPasswordChanged');
        if (token === '改角色') return t('security.detailRoleChanged');
        return null;
      });
      if (tokens.every((v) => v !== null)) {
        return tokens.join(t('security.detailChangedSeparator'));
      }
      return part;
    })
    .join(t('security.detailSeparator'));
}

/**
 * 拦截原因短码的**兜底映射**：后端已并入 keysvc 的 code，加上网关自己的三个。
 *
 * 为什么要在前端做一次映射而不是后端直接存译文：日志表存的是稳定短码，
 * 界面切成英文时同一批记录要显示英文——存译文就做不到（要么历史记录永远是
 * 中文，要么改文案得迁移数据）。
 *
 * 认不出的码**原样显示**：将来后端加了新原因，界面不会显示成空白或「未知」，
 * 用户至少能看到那个码（拿它搜仓库能找到定义）。
 */
const REASON_KEYS: Record<string, string> = {
  // 网关层
  missing_key: 'security.reasonMissingKey',
  invalid_key: 'security.reasonInvalidKey',
  ip_blocked: 'security.reasonIpBlocked',
  rate_limited: 'security.reasonRateLimited',
  // 密钥层（keysvc 的 code）
  key_disabled: 'security.reasonKeyDisabled',
  key_expired: 'security.reasonKeyExpired',
  quota_exhausted: 'security.reasonQuotaExhausted',
  credit_quota_exhausted: 'security.reasonCreditQuota',
  ip_not_allowed: 'security.reasonIpNotAllowed',
  too_many_ips: 'security.reasonTooManyIps',
  realm_mismatch: 'security.reasonRealmMismatch',
  model_not_allowed: 'security.reasonModelNotAllowed',
};

function reasonText(t: (key: string, params?: Record<string, string>) => string, code: string): string {
  const key = REASON_KEYS[code];
  return key ? t(key) : code;
}

export default function SecurityPage() {
  const {isAdmin} = useAuth();
  const t = useT();
  const [config, setConfig] = useState<SecurityConfig>({enabled: false, mode: 'blacklist'});
  const [rules, setRules] = useState<IpRule[]>([]);
  const [logs, setLogs] = useState<IpAccessLog[]>([]);
  const [loading, setLoading] = useState(true);
  /** 管理端审计日志：登录、改密码、增删用户等敏感操作留痕 */
  const [audit, setAudit] = useState<AuditLog[]>([]);

  const [newKind, setNewKind] = useState<'allow' | 'deny'>('deny');
  const [newCidr, setNewCidr] = useState('');
  const [newNote, setNewNote] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const [c, r, l, a] = await Promise.allSettled([
      securityApi.config(),
      securityApi.rules(),
      securityApi.logs(200),
      settingsApi.auditLogs(200),
    ]);
    if (c.status === 'fulfilled') setConfig(c.value);
    if (r.status === 'fulfilled') setRules(r.value);
    if (l.status === 'fulfilled') setLogs(l.value);
    if (a.status === 'fulfilled') setAudit(a.value.items);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // IP 规则与访问日志会随流量变化，心跳刷新保持同步
  useHeartbeat(load, 60000);

  async function saveConfig(next: SecurityConfig) {
    // 乐观更新：先切到目标态让开关立刻响应；失败则回滚到改动前的值，
    // 否则界面会停在一个后端并未生效的状态上（刷新才暴露）。
    const prev = config;
    setConfig(next);
    try {
      await securityApi.saveConfig(next);
      notify.ok(t('security.configSaved'));
    } catch (e) {
      setConfig(prev);
      notify.err(errText(e));
    }
  }

  async function addRule() {
    if (!newCidr.trim()) {
      notify.err(t('security.ipRequired'));
      return;
    }
    setBusy(true);
    try {
      await securityApi.addRule({kind: newKind, cidr: newCidr.trim(), note: newNote.trim()});
      notify.ok(t('security.ruleAdded'));
      setNewCidr('');
      setNewNote('');
      load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('security.title')}
        description={t('security.description')}
      />

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-1">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="h-4 w-4" />
            {t('security.policy')}
          </div>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs font-medium">{t('security.enableControl')}</div>
                <div className="text-[11px] text-muted-foreground">{t('security.enableControlHint')}</div>
              </div>
              <Switch
                checked={config.enabled}
                disabled={!isAdmin}
                onCheckedChange={(v) => saveConfig({...config, enabled: v})}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('security.mode')}</Label>
              <Select
                value={config.mode}
                disabled={!isAdmin}
                onValueChange={(v) => saveConfig({...config, mode: v as SecurityConfig['mode']})}
              >
                <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="blacklist">{t('security.modeBlacklist')}</SelectItem>
                  <SelectItem value="whitelist">{t('security.modeWhitelist')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Network className="h-4 w-4" />
            {t('security.addRule')}
          </div>
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('security.type')}</Label>
              <Select value={newKind} onValueChange={(v) => setNewKind(v as 'allow' | 'deny')} disabled={!isAdmin}>
                <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="deny">{t('security.deny')}</SelectItem>
                  <SelectItem value="allow">{t('security.allow')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">IP / CIDR</Label>
              <Input value={newCidr} onChange={(e) => setNewCidr(e.target.value)} placeholder={t('security.cidrPlaceholder')} className="bg-background" disabled={!isAdmin} />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('security.note')}</Label>
              <Input value={newNote} onChange={(e) => setNewNote(e.target.value)} placeholder={t('common.optional')} className="bg-background" disabled={!isAdmin} />
            </div>
            <Button className="rounded-full" onClick={addRule} disabled={!isAdmin || busy}>
              <Plus />
              {t('security.add')}
            </Button>
          </div>

          <div className="mt-4 overflow-hidden rounded-2xl bg-background/60">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-3 text-[11px] text-muted-foreground">{t('security.type')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">IP / CIDR</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('security.note')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('security.createdAt')}</TableHead>
                  {isAdmin && <TableHead className="pr-3 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rules.map((r) => (
                  <TableRow key={r.id} className="border-b border-border/40">
                    <TableCell className="pl-3">
                      {r.kind === 'allow' ? (
                        <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                          <CircleCheck className="h-3 w-3" />{t('security.allow')}
                        </Badge>
                      ) : (
                        <Badge variant="destructive" className="rounded-full">
                          <Ban className="h-3 w-3" />{t('security.deny')}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{r.cidr}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{r.note || '—'}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{fmtDateTime(r.created_at)}</TableCell>
                    {isAdmin && (
                      <TableCell className="pr-3 text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                          onClick={async () => {
                            try {
                              await securityApi.removeRule(r.id);
                              notify.ok(t('keys.deleted'));
                              load();
                            } catch (e) {
                              notify.err(errText(e));
                            }
                          }}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!rules.length && (
              <div className="py-8 text-center text-xs text-muted-foreground">{t('security.noRules')}</div>
            )}
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm font-medium">{t('security.accessLog')}</span>
            {/* 说明这张表**只记拦截**：用户看到记录变少时会以为是坏了，
                而实际上是被刻意收窄到安全信号（放行的明细在「请求日志」页）。
                不写这句，这个改动本身就是个新的困惑源。 */}
            <span className="text-[11px] text-muted-foreground">
              {t('security.accessLogScope')}
            </span>
          </div>
          {isAdmin && (
            <ConfirmDialog
              title={t('security.clearLogsTitle')}
              description={t('security.clearLogsDesc')}
              confirmText={t('common.clear')}
              destructive
              onConfirm={async () => {
                await securityApi.logsClear();
                notify.ok(t('logs.cleared'));
                load();
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-red-500">
                  <Trash2 />
                  {t('common.clear')}
                </Button>
              }
            />
          )}
        </div>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('security.path')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('security.result')}</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">User-Agent</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow key={l.id} className="border-b border-border/40">
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTime(l.ts)}</TableCell>
                <TableCell className="font-mono text-xs">{l.ip}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{l.path}</TableCell>
                <TableCell>
                  {/* 原因直接跟在「已拦截」后面：只有「已拦截」两个字时，用户
                      不知道是没带密钥、密钥不认识还是 IP 规则拦的，而这三者的
                      处置方式完全不同（改客户端配置 / 重新发密钥 / 改规则）。
                      没有原因（放行，或升级前没记）时不占位。 */}
                  <div className="flex flex-wrap items-center gap-1.5">
                    {l.blocked ? (
                      <Badge variant="destructive" className="rounded-full">{t('security.blocked')}</Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{t('security.allowed')}</Badge>
                    )}
                    {l.blocked && l.reason && (
                      <span className="text-[11px] text-muted-foreground">{reasonText(t, l.reason)}</span>
                    )}
                  </div>
                </TableCell>
                <TableCell className="max-w-[280px] truncate pr-4 text-[11px] text-muted-foreground">
                  {l.ua || '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!logs.length && !loading && (
          <EmptyState
            icon={ShieldCheck}
            title={t('security.noAccessLogs')}
            description={t('security.noAccessLogsDesc')}
            className="flex flex-col items-center justify-center py-14 text-center"
          />
        )}
      </section>

      {/* 管理端审计日志：本次安全事件暴露的问题之一就是「改密码不留痕」，
          只能靠反代日志去猜。这里把敏感操作（登录成败、改密码、增删用户）
          长期留痕，便于事后追溯与发现异常尝试。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <FileClock className="h-4 w-4" />
            {t('security.auditLog')}
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              {t('security.auditLogHint')}
            </span>
          </div>
          <Badge variant="secondary" className="shrink-0 rounded-full tabular-nums">
            {t('security.auditCount', {count: audit.length, n: audit.length})}
          </Badge>
        </div>
        {audit.length ? (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.action')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.actor')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.target')}</TableHead>
                <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('security.detail')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {audit.map((a) => (
                <TableRow key={a.id} className="border-b border-border/40">
                  <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
                    {fmtDateTime(a.ts)}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="secondary"
                      className={
                        'rounded-full text-[10px] ' +
                        (a.action === 'login_failed'
                          ? 'bg-red-500/12 text-red-600 dark:text-red-400'
                          : a.action === 'login'
                            ? 'text-emerald-600 dark:text-emerald-400'
                            : '')
                      }
                    >
                      {AUDIT_LABEL_KEYS[a.action] ? t(AUDIT_LABEL_KEYS[a.action]) : a.action}
                    </Badge>
                  </TableCell>
                  {/* 登录失败时后端把「没填用户名」记成 (空) —— 这个标记也要跟着界面语言走 */}
                  <TableCell className="text-xs">
                    {a.actor === '(空)' ? t('security.actorEmpty') : a.actor || '—'}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{a.target || '—'}</TableCell>
                  <TableCell className="max-w-[420px] truncate pr-4 text-[11px] text-muted-foreground" title={a.detail}>
                    {auditDetail(a.detail, t)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          !loading && (
            <EmptyState
              icon={FileClock}
              title={t('security.noAudit')}
              description={t('security.noAuditDesc')}
              className="flex flex-col items-center justify-center py-14 text-center"
            />
          )
        )}
      </section>
    </div>
  );
}
