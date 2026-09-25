'use client';

import {useCallback, useEffect, useState} from 'react';
import {Gift, Plus, Copy, Check, Download, Undo2, Link2, RefreshCw} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {redPacketApi, errText} from '@/lib/api';
import type {
  CreatedRedPacket, RedPacket, RedPacketDetail, RedPacketKind, RedPacketMode,
} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle,
} from '@/components/ui/drawer';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table';
import {useT} from '@/lib/i18n/provider';

/** 默认有效期（天）。与后端 redpacket.DEFAULT_TTL_DAYS 一致。 */
const DEFAULT_TTL_DAYS = 7;

/**
 * 单位换算与展示。
 *
 * 积分是小数（上游 credit 本身就是小数），token 是整数 —— 两者共用一个
 * 输入框和列表时，格式化必须跟着类别走，否则「500 token」会显示成「500.00」，
 * 而「0.05 积分」会显示成「0」。
 */
function fmtAmount(v: number, kind: RedPacketKind): string {
  return kind === 'token' ? fmtNumber(v) : v.toFixed(2);
}

export default function RedPacketsPage() {
  const t = useT();
  const {isAdmin} = useAuth();

  const [list, setList] = useState<RedPacket[]>([]);
  const [loading, setLoading] = useState(true);
  /** 创建结果 —— 含**明文 key**，只在这一刻有；关掉就再也拿不到。 */
  const [created, setCreated] = useState<CreatedRedPacket | null>(null);
  const [detail, setDetail] = useState<RedPacketDetail | null>(null);
  const [busy, setBusy] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [kind, setKind] = useState<RedPacketKind>('credit');
  const [total, setTotal] = useState('100');
  const [shares, setShares] = useState('5');
  const [mode, setMode] = useState<RedPacketMode>('lucky');
  const [ttlDays, setTtlDays] = useState(String(DEFAULT_TTL_DAYS));
  /** 模型白名单（逗号或换行分隔）。**token 红包必填、积分红包不显示**。 */
  const [models, setModels] = useState('');

  const load = useCallback(async () => {
    try {
      setList(await redPacketApi.list());
    } catch (e) {
      notify.err(t('redPacket.loadFailed'), errText(e));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);
  useHeartbeat(load, 30000);

  async function submit() {
    const amount = Number(total);
    const count = Number(shares);
    const ttl = Number(ttlDays);
    // 前端只做最基本的拦（空值/非数字）；**业务校验一律在后端**
    // （总额够不够分、份数上限…）——前端再算一遍就是第二份事实来源，
    // 两边迟早不一致。
    if (!Number.isFinite(amount) || amount <= 0) {
      notify.warn(t('redPacket.errTotal'));
      return;
    }
    if (!Number.isInteger(count) || count < 1) {
      notify.warn(t('redPacket.errShares'));
      return;
    }
    // token 红包必须限定模型（后端也拦，这里先给个更快的反馈）。
    // 拆法与密钥页同款：逗号或换行都认，去空白去空项。
    const modelList = kind === 'token'
      ? models.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
      : [];
    if (kind === 'token' && modelList.length === 0) {
      notify.warn(t('redPacket.errModels'));
      return;
    }
    setBusy(true);
    try {
      const out = await redPacketApi.create({
        title: title.trim(),
        quota_kind: kind,
        total_amount: amount,
        shares: count,
        mode,
        ttl_days: Number.isInteger(ttl) && ttl > 0 ? ttl : null,
        models: modelList,
      });
      setCreated(out);
      setFormOpen(false);
      setTitle('');
      await load();
    } catch (e) {
      notify.err(t('redPacket.createFailed'), errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function openDetail(p: RedPacket) {
    try {
      setDetail(await redPacketApi.detail(p.id));
    } catch (e) {
      notify.err(t('redPacket.loadFailed'), errText(e));
    }
  }

  /** 明文 key 的全部文本（复制/导出共用一份拼装逻辑）。 */
  function plainText(pack: CreatedRedPacket): string {
    // `quota` 在类型上是 `number | null`（后端 0 = 不限，但字段可空），
    // 所以按类别取的时候要显式兜底，否则拼出来是 "null"。
    return pack.keys
      .map((k) => {
        const amount = pack.quota_kind === 'token' ? (k.quota ?? 0) : k.quota_credit;
        return `${k.key}\t${fmtAmount(amount, pack.quota_kind)}`;
      })
      .join('\n');
  }

  /**
   * 抽奖链接。用 query 参数而不是路径段：静态导出下动态路径要预生成所有可能的
   * code（不可能），而 query 不需要任何路由支持 —— `/claim/` 是普通静态页。
   */
  function claimUrl(code: string): string {
    return `${window.location.origin}/claim/?code=${encodeURIComponent(code)}`;
  }

  async function copyLink(code: string) {
    try {
      await navigator.clipboard.writeText(claimUrl(code));
      notify.ok(t('redPacket.linkCopied'));
    } catch {
      // 如实说失败：谎报成功会让用户去粘贴一个空剪贴板（项目里踩过这个坑）
      notify.err(t('redPacket.copyFailed'), t('redPacket.copyFailedDetail'));
    }
  }

  async function copyAll(pack: CreatedRedPacket) {
    const text = plainText(pack);
    try {
      await navigator.clipboard.writeText(text);
      notify.ok(t('redPacket.copiedAll'), t('redPacket.copiedAllDetail', {n: pack.keys.length}));
    } catch {
      // 浏览器拒绝写剪贴板（非 HTTPS / 权限）时**如实说失败** ——
      // 谎报成功会让用户贴出来是空的（这个坑项目里踩过）。
      notify.err(t('redPacket.copyFailed'), t('redPacket.copyFailedDetail'));
    }
  }

  function exportCsv(pack: CreatedRedPacket) {
    // 加 BOM：Excel 打开 UTF-8 CSV 才不会把中文变成乱码
    const rows = [
      ['key', pack.quota_kind === 'token' ? 'tokens' : 'credits'],
      ...pack.keys.map((k) => [
        k.key,
        String(pack.quota_kind === 'token' ? (k.quota ?? 0) : k.quota_credit),
      ]),
    ];
    const csv = '﻿' + rows.map((r) => r.join(',')).join('\n');
    const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
    const a = document.createElement('a');
    a.href = url;
    a.download = `red-packet-${pack.id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={t('redPacket.title')}
        description={t('redPacket.desc')}
        actions={
          isAdmin ? (
            <Button className="rounded-full" onClick={() => setFormOpen((v) => !v)}>
              <Plus className="mr-1.5 h-4 w-4" />
              {t('redPacket.new')}
            </Button>
          ) : null
        }
      />

      {/* 创建表单 */}
      {formOpen && isAdmin && (
        <section className="rounded-[20px] bg-muted px-3.5 py-3">
          <div className="mb-2.5 text-sm font-medium">{t('redPacket.formTitle')}</div>
          <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldKind')}</Label>
              <Select value={kind} onValueChange={(v) => setKind(v as RedPacketKind)}>
                <SelectTrigger className="h-8 w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="credit">{t('redPacket.kindCredit')}</SelectItem>
                  <SelectItem value="token">{t('redPacket.kindToken')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldMode')}</Label>
              <Select value={mode} onValueChange={(v) => setMode(v as RedPacketMode)}>
                <SelectTrigger className="h-8 w-44"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="lucky">{t('redPacket.modeLucky')}</SelectItem>
                  <SelectItem value="even">{t('redPacket.modeEven')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldTotal')}</Label>
              <Input className="h-8 w-44 bg-background text-right tabular-nums"
                     value={total} onChange={(e) => setTotal(e.target.value)} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldShares')}</Label>
              <Input className="h-8 w-44 bg-background text-right tabular-nums"
                     value={shares} onChange={(e) => setShares(e.target.value)} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldTtl')}</Label>
              <Input className="h-8 w-44 bg-background text-right tabular-nums"
                     value={ttlDays} onChange={(e) => setTtlDays(e.target.value)} />
            </div>
            <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
              <Label className="text-xs">{t('redPacket.fieldTitle')}</Label>
              <Input className="h-8 w-44 bg-background" value={title}
                     placeholder={t('redPacket.fieldTitlePlaceholder')}
                     onChange={(e) => setTitle(e.target.value)} />
            </div>

            {/* 模型范围只在 token 红包下出现 —— 积分红包**不限制模型**
                （按真实扣费计，任何模型都能用），显示出来反而会让人以为
                可以限制。两类的规则相反是刻意的，见 redpacket.validate。 */}
            {kind === 'token' && (
              <div className="rounded-2xl bg-background/60 px-3 py-2 xl:col-span-2">
                <Label className="text-xs">{t('redPacket.fieldModels')}</Label>
                <Textarea
                  className="mt-1 min-h-16 bg-background text-xs"
                  value={models}
                  placeholder={t('redPacket.fieldModelsPlaceholder')}
                  onChange={(e) => setModels(e.target.value)}
                />
                <div className="mt-1 text-[10px] leading-3 text-muted-foreground">
                  {t('redPacket.fieldModelsHint')}
                </div>
              </div>
            )}
          </div>
          <div className="mt-2.5 flex justify-end gap-2">
            <Button variant="ghost" className="rounded-full" disabled={busy}
                    onClick={() => setFormOpen(false)}>{t('common.cancel')}</Button>
            <Button className="rounded-full" disabled={busy} onClick={submit}>
              <Gift className="mr-1.5 h-4 w-4" />{t('redPacket.create')}
            </Button>
          </div>
        </section>
      )}

      {/* 创建结果 —— 明文 key 只此一次 */}
      {created && (
        <section className="rounded-[20px] border border-amber-500/40 bg-amber-500/5 px-3.5 py-3">
          <div className="mb-1 text-sm font-medium">
            {t('redPacket.resultTitle', {n: created.keys.length})}
          </div>
          <div className="mb-2 text-[11px] text-amber-600 dark:text-amber-400">
            {t('redPacket.resultWarn')}
          </div>

          {/* 抽奖链接：把链接发出去让每个人自己抽（每个 IP 一次）。
              与「自己拿 key 去发」是两条路——用哪条都行，也可以只用其中一条。
              用 query 参数而不是路径段：静态导出下动态路径要预生成所有可能的
              code（不可能），query 不需要任何路由支持。 */}
          {created.code && (
            <div className="mb-2 rounded-2xl bg-background/60 px-3 py-2">
              <div className="mb-1 text-[11px] text-muted-foreground">
                {t('redPacket.claimLink')}
              </div>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate text-[11px]">
                  {claimUrl(created.code)}
                </code>
                <Button
                  size="sm"
                  variant="outline"
                  className="shrink-0 rounded-full"
                  onClick={() => copyLink(created.code)}
                >
                  <Copy className="mr-1 h-3 w-3" />{t('redPacket.copyLink')}
                </Button>
              </div>
              <div className="mt-1 text-[10px] leading-4 text-muted-foreground">
                {t('redPacket.claimLinkHint')}
              </div>
            </div>
          )}
          <div className="max-h-64 overflow-auto rounded-2xl bg-background/60 p-2 font-mono text-[11px] leading-5">
            {created.keys.map((k) => (
              <div key={k.id} className="flex items-center justify-between gap-2">
                <span className="truncate">{k.key}</span>
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {fmtAmount(created.quota_kind === 'token' ? (k.quota ?? 0) : k.quota_credit,
                             created.quota_kind)}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-2.5 flex flex-wrap justify-end gap-2">
            <Button variant="outline" className="rounded-full" onClick={() => copyAll(created)}>
              <Copy className="mr-1.5 h-4 w-4" />{t('redPacket.copyAll')}
            </Button>
            <Button variant="outline" className="rounded-full" onClick={() => exportCsv(created)}>
              <Download className="mr-1.5 h-4 w-4" />{t('redPacket.exportCsv')}
            </Button>
            <Button className="rounded-full" onClick={() => setCreated(null)}>
              <Check className="mr-1.5 h-4 w-4" />{t('redPacket.done')}
            </Button>
          </div>
        </section>
      )}

      {/* 列表 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        {loading ? (
          <div className="flex items-center justify-center py-10 text-muted-foreground">
            <RefreshCw className="mr-2 h-4 w-4 animate-spin" />{t('common.loading')}
          </div>
        ) : list.length === 0 ? (
          <EmptyState
            icon={Gift}
            title={t('redPacket.empty')}
            description={t('redPacket.emptyDesc')}
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px]">{t('redPacket.colTitle')}</TableHead>
                <TableHead className="text-[11px]">{t('redPacket.colKind')}</TableHead>
                <TableHead className="text-[11px]">{t('redPacket.colTotal')}</TableHead>
                <TableHead className="text-[11px]">{t('redPacket.colShares')}</TableHead>
                <TableHead className="text-[11px]">{t('redPacket.colMode')}</TableHead>
                <TableHead className="text-[11px]">{t('redPacket.colExpires')}</TableHead>
                <TableHead className="pr-4 text-[11px]">{t('accounts.colStatus')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.map((p) => (
                <TableRow key={p.id} className="cursor-pointer border-b border-border/40"
                          onClick={() => openDetail(p)}>
                  <TableCell className="pl-4 text-xs">{p.title || '—'}</TableCell>
                  <TableCell className="text-xs">
                    {p.quota_kind === 'token'
                      ? t('redPacket.kindTokenShort')
                      : t('redPacket.kindCreditShort')}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {fmtAmount(p.total_amount, p.quota_kind)}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">{p.shares}</TableCell>
                  <TableCell className="text-xs">
                    {p.mode === 'lucky' ? t('redPacket.modeLuckyShort') : t('redPacket.modeEvenShort')}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {fmtDateTime(p.expires_at)}
                  </TableCell>
                  <TableCell className="pr-4">
                    <div className="flex items-center gap-1.5">
                      {/* 已领取 / 总量。抽奖式红包一眼要看的是这个进度 ——
                          「还剩几份」比「谁发的」重要得多。 */}
                      <span
                        className="shrink-0 rounded-full bg-background px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground"
                        title={t('redPacket.claimedTip', {n: p.claimed, total: p.shares})}
                      >
                        {p.claimed}/{p.shares}
                      </span>

                      {p.revoked ? (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {t('redPacket.revokedTag')}
                        </span>
                      ) : (
                        isAdmin && (
                          <>
                            {/* 复制抽奖链接。没有码的老红包在启动时会被补一个
                                （见 redpacket.backfill_codes），所以这里一般都有。 */}
                            {p.code ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-6 rounded-full px-2 text-xs"
                                title={t('redPacket.copyLink')}
                                onClick={(e) => { e.stopPropagation(); void copyLink(p.code); }}
                              >
                                <Link2 className="h-3 w-3" />
                              </Button>
                            ) : null}

                            {/* ConfirmDialog 是 **trigger 式**的（开关状态在组件内部），
                                所以把按钮作为 trigger 传进去，不要再在外面维护一份 open。
                                stopPropagation 仍然需要：这一列在可点击的 TableRow 里，
                                不拦的话点「收回」会顺带打开详情抽屉。 */}
                            <span onClick={(e) => e.stopPropagation()}>
                              <ConfirmDialog
                                destructive
                                trigger={
                                  <Button size="sm" variant="ghost"
                                          className="h-6 rounded-full text-xs">
                                    <Undo2 className="mr-1 h-3 w-3" />{t('redPacket.revoke')}
                                  </Button>
                                }
                                title={t('redPacket.confirmRevoke')}
                                description={t('redPacket.confirmRevokeDesc', {n: p.shares})}
                                confirmText={t('redPacket.revoke')}
                                onConfirm={async () => {
                                  try {
                                    const r = await redPacketApi.revoke(p.id);
                                    notify.ok(t('redPacket.revoked'),
                                              t('redPacket.revokedDetail', {n: r.revoked}));
                                    await load();
                                  } catch (e) {
                                    notify.err(t('redPacket.revokeFailed'), errText(e));
                                  }
                                }}
                              />
                            </span>
                          </>
                        )
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      {/* 详情 */}
      <Drawer open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DrawerContent>
          <DrawerHeader>
            <DrawerTitle>{detail?.title || t('redPacket.detailTitle')}</DrawerTitle>
            <DrawerDescription>
              {detail && (
                <>
                  {t('redPacket.detailSub', {
                    total: fmtAmount(detail.total_amount, detail.quota_kind),
                    n: detail.shares,
                    at: fmtDateTime(detail.expires_at),
                  })}
                  {/* token 红包才有这一行（积分红包恒为空） */}
                  {detail.models?.length ? (
                    <span className="mt-0.5 block">
                      {t('redPacket.modelsScoped', {list: detail.models.join(', ')})}
                    </span>
                  ) : null}
                </>
              )}
            </DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="max-h-[60vh] space-y-2 overflow-auto px-4 pb-8 text-xs">
              {detail.items.map((it) => (
                <div key={it.key_id}
                     className="flex items-center justify-between gap-3 rounded-2xl bg-muted px-3 py-2">
                  <span className="font-mono text-[11px]">{it.prefix}…</span>
                  <span className="tabular-nums">{fmtAmount(it.amount, detail.quota_kind)}</span>
                  <span className="text-muted-foreground">
                    {detail.quota_kind === 'token'
                      ? t('redPacket.usedTokens', {n: fmtNumber(it.used_tokens)})
                      : t('redPacket.usedCredit', {n: it.used_credit.toFixed(2)})}
                  </span>
                  {!it.enabled && (
                    <span className="rounded-full bg-muted-foreground/15 px-1.5 py-0.5 text-[10px]">
                      {t('redPacket.revokedTag')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </DrawerContent>
      </Drawer>

    </div>
  );
}
