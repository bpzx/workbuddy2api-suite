'use client';

import {useCallback, useEffect, useState} from 'react';
import {ShieldCheck, Plus, Trash2, Ban, CircleCheck, Network} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
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

const AUDIT_LABELS: Record<string, string> = {
  login: '登录成功',
  login_failed: '登录失败',
  update_user: '修改用户',
  delete_user: '删除用户',
  add_user: '新增用户',
};

export default function SecurityPage() {
  const {isAdmin} = useAuth();
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
      notify.ok('安全配置已保存');
    } catch (e) {
      setConfig(prev);
      notify.err(errText(e));
    }
  }

  async function addRule() {
    if (!newCidr.trim()) {
      notify.err('请输入 IP 或 CIDR');
      return;
    }
    setBusy(true);
    try {
      await securityApi.addRule({kind: newKind, cidr: newCidr.trim(), note: newNote.trim()});
      notify.ok('规则已添加');
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
        title="安全与 IP 管控"
        description="入站 IP 白/黑名单、访问审计与全局拦截开关（每 60 秒自动刷新）"
      />

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-[20px] bg-muted p-4 lg:col-span-1">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <ShieldCheck className="h-4 w-4" />
            拦截策略
          </div>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-xs font-medium">启用 IP 管控</div>
                <div className="text-[11px] text-muted-foreground">关闭时放行所有来源 IP</div>
              </div>
              <Switch
                checked={config.enabled}
                disabled={!isAdmin}
                onCheckedChange={(v) => saveConfig({...config, enabled: v})}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">模式</Label>
              <Select
                value={config.mode}
                disabled={!isAdmin}
                onValueChange={(v) => saveConfig({...config, mode: v as SecurityConfig['mode']})}
              >
                <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="blacklist">黑名单（仅拒绝命中项，默认放行）</SelectItem>
                  <SelectItem value="whitelist">白名单（仅放行命中项，默认拒绝）</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>

        <div className="rounded-[20px] bg-muted p-4 lg:col-span-2">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium">
            <Network className="h-4 w-4" />
            添加规则
          </div>
          <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">类型</Label>
              <Select value={newKind} onValueChange={(v) => setNewKind(v as 'allow' | 'deny')} disabled={!isAdmin}>
                <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="deny">拒绝</SelectItem>
                  <SelectItem value="allow">放行</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">IP / CIDR</Label>
              <Input value={newCidr} onChange={(e) => setNewCidr(e.target.value)} placeholder="1.2.3.4 或 10.0.0.0/8" className="bg-background" disabled={!isAdmin} />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">备注</Label>
              <Input value={newNote} onChange={(e) => setNewNote(e.target.value)} placeholder="可选" className="bg-background" disabled={!isAdmin} />
            </div>
            <Button className="rounded-full" onClick={addRule} disabled={!isAdmin || busy}>
              <Plus />
              添加
            </Button>
          </div>

          <div className="mt-4 overflow-hidden rounded-2xl bg-background/60">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-3 text-[11px] text-muted-foreground">类型</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">IP / CIDR</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">备注</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">创建时间</TableHead>
                  {isAdmin && <TableHead className="pr-3 text-right text-[11px] text-muted-foreground">操作</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rules.map((r) => (
                  <TableRow key={r.id} className="border-b border-border/40">
                    <TableCell className="pl-3">
                      {r.kind === 'allow' ? (
                        <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                          <CircleCheck className="h-3 w-3" />放行
                        </Badge>
                      ) : (
                        <Badge variant="destructive" className="rounded-full">
                          <Ban className="h-3 w-3" />拒绝
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
                              notify.ok('已删除');
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
              <div className="py-8 text-center text-xs text-muted-foreground">暂无规则</div>
            )}
          </div>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-sm font-medium">IP 访问日志</div>
          {isAdmin && (
            <ConfirmDialog
              title="清空访问日志？"
              description="将删除全部 IP 访问审计记录。"
              confirmText="清空"
              destructive
              onConfirm={async () => {
                await securityApi.logsClear();
                notify.ok('已清空');
                load();
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-red-500">
                  <Trash2 />
                  清空
                </Button>
              }
            />
          )}
        </div>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">时间</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">路径</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">结果</TableHead>
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
                  {l.blocked ? (
                    <Badge variant="destructive" className="rounded-full">已拦截</Badge>
                  ) : (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">放行</Badge>
                  )}
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
            title="暂无访问记录"
            description="网关收到请求后会在此留痕"
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
            管理端审计日志
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              登录 / 改密码 / 增删用户等敏感操作
            </span>
          </div>
          <Badge variant="secondary" className="shrink-0 rounded-full tabular-nums">
            共 {audit.length} 条
          </Badge>
        </div>
        {audit.length ? (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px] text-muted-foreground">时间</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">操作</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">操作者</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">对象</TableHead>
                <TableHead className="pr-4 text-[11px] text-muted-foreground">详情</TableHead>
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
                      {AUDIT_LABELS[a.action] || a.action}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs">{a.actor || '—'}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{a.target || '—'}</TableCell>
                  <TableCell className="max-w-[420px] truncate pr-4 text-[11px] text-muted-foreground" title={a.detail}>
                    {a.detail || '—'}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          !loading && (
            <EmptyState
              icon={FileClock}
              title="暂无审计记录"
              description="登录、改密码、增删用户等操作会在此留痕（升级后开始记录）"
              className="flex flex-col items-center justify-center py-14 text-center"
            />
          )
        )}
      </section>
    </div>
  );
}
