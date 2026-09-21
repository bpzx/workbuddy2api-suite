'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {ScrollText, Search, Trash2, ChevronLeft, ChevronRight} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keyApi, logApi, errText} from '@/lib/api';
import type {ApiKey, RequestLog} from '@/lib/types';
import {fmtCredit, fmtDateTime, fmtDateTimeMarked, fmtLatency, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const PAGE_SIZE = 20;

export default function LogsPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  // 日志随顶部版本切换：两个版本走不同账号池，混看会把两个池子的调用搅在一起
  const {realm, label: realmName} = useRealm();
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [detail, setDetail] = useState<RequestLog | null>(null);

  const [keyId, setKeyId] = useState('all');
  const [model, setModel] = useState('');
  const [status, setStatus] = useState('all');
  const [ip, setIp] = useState('');
  const [days, setDays] = useState('7');

  /**
   * 切版本时回到第 1 页。
   *
   * 为什么必须重置：两个版本的日志条数不同，停在「第 5 页」再切版本会去查另一个
   * 版本的第 5 页——那边可能只有 1 页，于是看到空白表格和「第 5 页 / 共 1 页」
   * 这种自相矛盾的页码。其余筛选项（天数 / 密钥 / 状态）都是这么做的，版本不该例外。
   *
   * 写法说明：用「渲染期纠正 state」而不是 `useEffect` + `setPage`。
   * 后者会先用旧页码发一次请求、再重置页码发第二次（白白多查一次，且第一份
   * 结果可能短暂显示出来）。在渲染期直接 setState，React 会在本次渲染结束前
   * 立刻用新 state 重渲染，下面的加载 effect 只跑一次、拿到的就是第 1 页。
   */
  const lastRealm = useRef(realm);
  if (lastRealm.current !== realm) {
    lastRealm.current = realm;
    if (page !== 1) setPage(1);
  }

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await logApi.list({
        page,
        size: PAGE_SIZE,
        key_id: keyId === 'all' ? undefined : keyId,
        model: model || undefined,
        status: status === 'all' ? undefined : status,
        ip: ip || undefined,
        days: Number(days) || undefined,
        realm,
      });
      setLogs(res.items);
      setTotal(res.total);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, [page, keyId, model, status, ip, days, realm]);

  useEffect(() => {
    load();
    // 依赖是「会改变查询范围」的项，而不是 load 本身：其余筛选条件（密钥/模型/
    // 状态/IP）由「查询」按钮显式触发，不该边打字边重查。
    //
    // **realm 必须在这里**：切版本若只等 60 秒心跳，用户点一下会觉得没反应、
    // 以为功能坏了（实测反馈）。分页与天数本来就在，切版本理应同属「立即重查」。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, days, realm]);

  // 新请求会不断写入日志；心跳刷新只更新当前筛选下的列表，不会重置筛选条件
  useHeartbeat(load, 60000);

  useEffect(() => {
    keyApi.list().then(setKeys).catch(() => undefined);
  }, []);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function applyFilters() {
    setPage(1);
    load();
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('logs.title')}
        description={t('logs.description')}
        actions={
          <>
            {isAdmin && (
              <ConfirmDialog
                title={t('logs.clearTitle')}
                description={t('logs.clearDesc')}
                confirmText={t('common.clear')}
                destructive
                onConfirm={async () => {
                  await logApi.clear();
                  notify.ok(t('logs.cleared'));
                  setPage(1);
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
          </>
        }
      />

      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterRange')}</Label>
            <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('logs.last24h')}</SelectItem>
                <SelectItem value="7">{t('stats.last7')}</SelectItem>
                <SelectItem value="30">{t('stats.last30')}</SelectItem>
                <SelectItem value="90">{t('stats.last90')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterKey')}</Label>
            <Select value={keyId} onValueChange={(v) => { setKeyId(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('logs.allKeys')}</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>{k.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</Label>
            <Select value={status} onValueChange={(v) => { setStatus(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('common.all')}</SelectItem>
                <SelectItem value="ok">{t('common.success')}</SelectItem>
                <SelectItem value="error">{t('common.failure')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('nav.models')}</Label>
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('common.all')} className="bg-background" />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterIp')}</Label>
            <Input value={ip} onChange={(e) => setIp(e.target.value)} placeholder={t('common.all')} className="bg-background" />
          </div>
          <Button className="rounded-full" onClick={applyFilters}>
            <Search />
            {t('logs.filter')}
          </Button>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('nav.keys')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('nav.models')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colFirstToken')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colLatency')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('metric.paid')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow
                key={l.id}
                className="cursor-pointer border-b border-border/40"
                onClick={() => setDetail(l)}
              >
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTimeMarked(l.ts)}</TableCell>
                <TableCell className="text-xs">{l.key_name || '—'}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{l.ip}</TableCell>
                <TableCell className="text-xs">
                  {l.model || '—'}
                  {l.mapped_model && l.mapped_model !== l.model && (
                    <span className="ml-1 text-[10px] text-muted-foreground">→{l.mapped_model}</span>
                  )}
                </TableCell>
                <TableCell>
                  {l.status >= 200 && l.status < 300 ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{l.status}</Badge>
                  ) : l.status >= 400 && l.status < 500 ? (
                    <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400">
                      {l.status || 'ERR'}
                    </Badge>
                  ) : (
                    <Badge variant="destructive" className="rounded-full">{l.status || 'ERR'}</Badge>
                  )}
                </TableCell>
                {/* 首字延迟：反映「上游多久开始回话」。回答越长总耗时越大，
                    所以判断上游快慢只看这一列。非流式请求没有中间过程，显示 —。 */}
                <TableCell
                  className={
                    'text-xs tabular-nums ' +
                    (l.first_token_ms != null && l.first_token_ms >= 3000
                      ? 'font-medium text-amber-600 dark:text-amber-400'
                      : 'text-muted-foreground')
                  }
                >
                  {l.first_token_ms != null ? (
                    fmtLatency(l.first_token_ms)
                  ) : (
                    <span className="text-muted-foreground/50">—</span>
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {fmtLatency(l.latency_ms)}
                </TableCell>
                <TableCell className="text-xs tabular-nums">
                  {l.prompt_tokens + l.completion_tokens > 0 ? (
                    fmtNumber(l.prompt_tokens + l.completion_tokens)
                  ) : (
                    <span className="text-muted-foreground/70">—</span>
                  )}
                  {l.stream && <span className="ml-1 text-[10px] text-muted-foreground">{t('logs.streamShort')}</span>}
                </TableCell>
                <TableCell className="pr-4 text-xs tabular-nums">
                  {typeof l.credit === 'number' ? (
                    <span className={l.credit > 0 ? 'text-amber-600 dark:text-amber-400' : ''}>
                      {fmtCredit(l.credit)}
                    </span>
                  ) : (
                    <span className="text-muted-foreground/70" title={t('logs.noCreditTitle')}>—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {!logs.length && !loading && (
          <EmptyState
            icon={ScrollText}
            title={t('logs.emptyTitle')}
            description={t('logs.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        )}

        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-[11px] text-muted-foreground">
            {t('logs.pageInfo', {total: fmtNumber(total), page, pages})}
          </div>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </section>

      <Drawer open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DrawerContent>
          <DrawerHeader>
            <DrawerTitle>{t('logs.detailTitle', {id: detail?.id ?? ''})}</DrawerTitle>
            <DrawerDescription>{detail ? fmtDateTime(detail.ts) : ''}</DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="space-y-3 px-4 pb-8 text-xs">
              {([
                ['ip', t('logs.rowIp'), detail.ip],
                ['key', t('logs.rowKey'), detail.key_name || '—'],
                ['model', t('logs.rowModel'), detail.model || '—'],
                ['mapped', t('logs.rowMappedModel'), detail.mapped_model || '—'],
                ['status', t('logs.rowStatus'), String(detail.status)],
                [
                  'firstToken',
                  t('logs.rowFirstToken'),
                  detail.first_token_ms != null
                    ? fmtLatency(detail.first_token_ms)
                    : t('logs.notCollected'),
                ],
                ['latency', t('logs.rowLatency'), fmtLatency(detail.latency_ms)],
                ['promptTokens', 'Prompt Token', fmtNumber(detail.prompt_tokens)],
                ['completionTokens', 'Completion Token', fmtNumber(detail.completion_tokens)],
                [
                  'credit',
                  t('logs.rowCredit'),
                  typeof detail.credit === 'number'
                    ? fmtCredit(detail.credit) + (detail.credit > 0 ? '' : t('logs.unbilled'))
                    : t('logs.noCredit'),
                ],
                ['stream', t('logs.rowStream'), detail.stream ? t('common.yes') : t('common.no')],
                ['ua', 'User-Agent', detail.ua || '—'],
                ['error', t('logs.rowError'), detail.error || '—'],
              ] as [string, string, string][]).map(([id, k, v]) => {
                // 这些字段内容较长且常需要贴出来（排查 / 反馈），给出复制入口
                const copyable = ['ip', 'ua', 'error'].includes(id) && v !== '—';
                return (
                  <div key={id} className="flex items-start gap-3">
                    <div className="w-32 shrink-0 text-muted-foreground">{k}</div>
                    <div className="min-w-0 flex-1 break-all font-mono">{v}</div>
                    {copyable && <CopyButton value={v} title={t('logs.copyField', {field: k})} className="-mt-1" />}
                  </div>
                );
              })}
            </div>
          )}
        </DrawerContent>
      </Drawer>
    </div>
  );
}
