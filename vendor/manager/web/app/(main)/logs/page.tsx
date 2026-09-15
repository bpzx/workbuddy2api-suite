'use client';

import {useCallback, useEffect, useState} from 'react';
import {ScrollText, Search, Trash2, ChevronLeft, ChevronRight} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keyApi, logApi, errText} from '@/lib/api';
import type {ApiKey, RequestLog} from '@/lib/types';
import {fmtCredit, fmtDateTime, fmtLatency, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
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
  const {isAdmin} = useAuth();
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
      });
      setLogs(res.items);
      setTotal(res.total);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, [page, keyId, model, status, ip, days]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, days]);

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
        title="请求日志"
        description="反代网关的每一次调用记录，含状态、延迟与 Token 计量（每 60 秒自动刷新；含国内版与国际版调用，不按版本过滤）"
        actions={
          <>
            {isAdmin && (
              <ConfirmDialog
                title="清空所有日志？"
                description="将删除全部请求日志记录，用量统计的汇总数据不受影响。"
                confirmText="清空"
                destructive
                onConfirm={async () => {
                  await logApi.clear();
                  notify.ok('已清空');
                  setPage(1);
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
          </>
        }
      />

      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">时间范围</Label>
            <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">近 24 小时</SelectItem>
                <SelectItem value="7">近 7 天</SelectItem>
                <SelectItem value="30">近 30 天</SelectItem>
                <SelectItem value="90">近 90 天</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">密钥</Label>
            <Select value={keyId} onValueChange={(v) => { setKeyId(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部密钥</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>{k.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">状态</Label>
            <Select value={status} onValueChange={(v) => { setStatus(v); setPage(1); }}>
              <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部</SelectItem>
                <SelectItem value="ok">成功</SelectItem>
                <SelectItem value="error">失败</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">模型</Label>
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="全部" className="bg-background" />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">来源 IP</Label>
            <Input value={ip} onChange={(e) => setIp(e.target.value)} placeholder="全部" className="bg-background" />
          </div>
          <Button className="rounded-full" onClick={applyFilters}>
            <Search />
            筛选
          </Button>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">时间</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">密钥</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">模型</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">状态</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">首字</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">总耗时</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">实付</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow
                key={l.id}
                className="cursor-pointer border-b border-border/40"
                onClick={() => setDetail(l)}
              >
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTime(l.ts)}</TableCell>
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
                  {l.stream && <span className="ml-1 text-[10px] text-muted-foreground">流</span>}
                </TableCell>
                <TableCell className="pr-4 text-xs tabular-nums">
                  {typeof l.credit === 'number' ? (
                    <span className={l.credit > 0 ? 'text-amber-600 dark:text-amber-400' : ''}>
                      {fmtCredit(l.credit)}
                    </span>
                  ) : (
                    <span className="text-muted-foreground/70" title="上游未返回该项（不等于免费）">—</span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {!logs.length && !loading && (
          <EmptyState
            icon={ScrollText}
            title="暂无日志"
            description="当有请求通过网关时，这里会显示调用记录"
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        )}

        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-[11px] text-muted-foreground">
            共 {fmtNumber(total)} 条 · 第 {page} / {pages} 页
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
            <DrawerTitle>请求详情 #{detail?.id}</DrawerTitle>
            <DrawerDescription>{detail ? fmtDateTime(detail.ts) : ''}</DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="space-y-3 px-4 pb-8 text-xs">
              {([
                ['来源 IP', detail.ip],
                ['密钥', detail.key_name || '—'],
                ['模型', detail.model || '—'],
                ['映射模型', detail.mapped_model || '—'],
                ['状态码', String(detail.status)],
                [
                  '首字延迟',
                  detail.first_token_ms != null
                    ? fmtLatency(detail.first_token_ms)
                    : '未采集（非流式请求）',
                ],
                ['总耗时', fmtLatency(detail.latency_ms)],
                ['Prompt Token', fmtNumber(detail.prompt_tokens)],
                ['Completion Token', fmtNumber(detail.completion_tokens)],
                [
                  '实际扣费',
                  typeof detail.credit === 'number'
                    ? fmtCredit(detail.credit) + (detail.credit > 0 ? '' : '（未计费）')
                    : '上游未返回',
                ],
                ['流式', detail.stream ? '是' : '否'],
                ['User-Agent', detail.ua || '—'],
                ['错误', detail.error || '—'],
              ] as [string, string][]).map(([k, v]) => {
                // 这些字段内容较长且常需要贴出来（排查 / 反馈），给出复制入口
                const copyable = ['来源 IP', 'User-Agent', '错误'].includes(k) && v !== '—';
                return (
                  <div key={k} className="flex items-start gap-3">
                    <div className="w-32 shrink-0 text-muted-foreground">{k}</div>
                    <div className="min-w-0 flex-1 break-all font-mono">{v}</div>
                    {copyable && <CopyButton value={v} title={`复制${k}`} className="-mt-1" />}
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
