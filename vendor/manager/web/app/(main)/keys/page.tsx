'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {KeyRound, Plus, Trash2, Ban, CircleCheck, Pencil, RotateCcw} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keyApi, errText} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import type {ApiKey} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {useRealm, type Realm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {Tabs, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

interface FormState {
  name: string;
  /** 新建：0 = 永不过期；编辑：0 = 保持当前有效期不变 */
  expiresDays: string;
  /** 编辑时的有效期操作：keep 保持不变 / days 从现在起 N 天 / never 永不过期 */
  expiryMode: 'keep' | 'days' | 'never';
  maxIps: string;
  ipAllowlist: string;
  models: string;
  quota: string;
  /** 积分额度（issue #27）：0 = 不限。按上游返回的**真实扣费**累计 */
  quotaCredit: string;
  /** 版本归属：'cn' | 'global' | ''（不限制，仅存量密钥） */
  realm: Realm | '';
}

const emptyForm: FormState = {
  name: '',
  expiryMode: 'keep',
  expiresDays: '0',
  maxIps: '0',
  ipAllowlist: '',
  models: '',
  quota: '0',
  quotaCredit: '0',
  realm: 'cn',
};

function toLines(v: string): string[] {
  return v
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function KeysPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const {realm, label: realmName} = useRealm();
  const [keys, setKeys] = useState<ApiKey[]>([]);
  /**
   * 列表分组。红包一次生成一批、额度零碎，与手工建的混在一起很难看。
   *
   * 默认停在「普通密钥」而不是「全部」：日常看的是自己发出去的那几把，
   * 红包那批是「发完就不太管」的；把它们混在首屏反而把常用的挤下去了。
   */
  const [tab, setTab] = useState<'normal' | 'packet'>('normal');
  const [loading, setLoading] = useState(true);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ApiKey | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);
  /**
   * 模型白名单里**匹配不到已知模型**的名字（issue #46）。
   *
   * 为什么要有：白名单是自由文本，填错不会报错，只会在下游表现为「模型列表是
   * 空的」——而空列表看不出原因（少个连字符？填成了显示名？）。所以在编辑处
   * 当场点出来。判据由后端给（与调用侧同一份），这里只负责显示。
   *
   * `null` = 还没查/查不了（拿不到模型清单）。**不能**把 null 当成「都没问题」——
   * 那会把「暂时查不了」显示成「全部正确」。
   */
  const [unknownModels, setUnknownModels] = useState<string[] | null>(null);
  /**
   * 提示元素的 ref：它出现时要**滚进可视区**。
   *
   * 为什么需要：模型白名单是弹窗里最后一个字段，提示挂在它下面——实测在
   * 常见窗口高度下，提示会落在弹窗内容区的**可视区之下**（被滚动容器裁掉），
   * 用户看不到任何东西。而这条提示的全部价值就是「当场看见」，看不见等于没做。
   * 用 `block:'nearest'`：只在需要时滚最小距离，不会把界面拽跑。
   */
  const unknownRef = useRef<HTMLParagraphElement | null>(null);
  /**
   * 提交锁（同步生效，与 `busy` 的渲染状态无关）。
   *
   * 为什么状态不够：`setBusy(true)` 要等下一次渲染才反映到按钮的 disabled 上，
   * 而快速连点/回车触发的第二次提交可能在那之前就进来了 —— 结果创建出**多把
   * 同名密钥**（实测：连点 3 次进 3 把）。用 ref 记录「已提交」，在当前这次
   * 调用栈里立即生效，不依赖渲染。
   *
   * 另外这把锁在 `finally` 里释放，失败（比如接口 500）时用户可以重试 ——
   * 不能因为一次报错就把表单永久锁死。
   */
  const submitting = useRef(false);

  /**
   * 拉取密钥列表。**返回是否成功** —— 调用方需要区分这两种失败。
   *
   * 为什么不能吞掉异常了事：创建成功后要刷新列表，若刷新失败而这里已把异常
   * 吃掉，`load().catch(...)` 永远不会触发，用户看到的是「创建失败」——于是
   * 他会再点一次，建出重复密钥（正是要避免的）。所以这里如实返回结果，
   * 由调用方决定怎么提示。
   */
  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      setKeys(await keyApi.list());
      return true;
    } catch (e) {
      notify.err(errText(e));
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 提示出现时确保它在可视区内（见 unknownRef 的说明）
  useEffect(() => {
    if (unknownModels && unknownModels.length) {
      unknownRef.current?.scrollIntoView({block: 'nearest'});
    }
  }, [unknownModels]);

  // 密钥状态可能被下游调用改变（配额用尽、过期），心跳刷新保持同步
  useHeartbeat(load, 60000);

  function openCreate() {
    setEditing(null);
    // 新建时默认跟随当前所在版本：在哪个版本的界面里建，就是哪个版本的密钥
    setForm({...emptyForm, realm});
    setUnknownModels(null);
    setFormOpen(true);
  }

  function openEdit(k: ApiKey) {
    setEditing(k);
    setForm({
      name: k.name,
      // 默认不改动有效期；要续期/取消过期需显式选择
      expiryMode: 'keep',
      expiresDays: '0',
      maxIps: String(k.max_ips || 0),
      ipAllowlist: (k.ip_allowlist || []).join('\n'),
      models: (k.models || []).join(', '),
      quota: String(k.quota ?? 0),
      quotaCredit: String(k.quota_credit ?? 0),
      realm: k.realm || '',
    });
    setUnknownModels(null);
    setFormOpen(true);
  }

  async function submit() {
    // 同步锁：必须在任何 await / setState 之前判断并置位。
    // 只看 `busy` 不够——那个状态要等重渲染才生效，连点会漏过去。
    if (submitting.current) return;
    if (!form.name.trim()) {
      notify.err(t('keys.nameRequired'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      const days = Number(form.expiresDays) || 0;
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        max_ips: Number(form.maxIps) || 0,
        ip_allowlist: toLines(form.ipAllowlist),
        models: toLines(form.models),
        quota: Number(form.quota) || 0,
        quota_credit: Number(form.quotaCredit) || 0,
        // '' 是有意义的取值（不限制版本），必须照传——后端以它区分
        // 「存量密钥，两版都能调」与「限定了某一版」
        //
        // 新建时**以当前所在版本为准**（而不是 openCreate 时的快照）：
        // 弹窗开着的时候用户可能切了版本，若沿用快照，创建出来的密钥版本
        // 会与界面上显示的不一致——那种错是静默的，只有调用时才暴露。
        realm: editing ? form.realm : realm,
      };

      // 新建：填了天数才设过期（0 = 永不过期，不下发 expires_at）
      // 编辑：按显式选择处理，避免「打开就保存」把有效期重置
      if (!editing) {
        if (days > 0) payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'days') {
        if (days <= 0) {
          notify.err(t('keys.daysRequired'));
          setBusy(false);
          return;
        }
        payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'never') {
        // 后端以 null 表示「无过期时间」
        payload.expires_at = null;
      }

      if (editing) {
        await keyApi.update(editing.id, payload as Partial<ApiKey>);
        notify.ok(t('keys.keyUpdated'));
      } else {
        const created = await keyApi.create(payload as Partial<ApiKey>);
        notify.ok(t('keys.keyCreated'));
        if (created.key) setIssued(created.key);
      }
      setFormOpen(false);
      // 刷新失败**不能**把这次创建判成失败：密钥已经建好了。
      // 所以这里用 load() 的返回值判断，而不是 `.catch()` —— load 内部已经
      // 把异常吃掉并弹了通用错误提示，返回的 Promise 永远不 reject，
      // 用 .catch 的话这段提示永远不会出现，用户只会看到「失败了」。
      if (!(await load())) {
        notify.warn(t('keys.createdButRefreshFailed'), t('keys.createdButRefreshFailedHint'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function toggle(k: ApiKey) {
    try {
      await keyApi.update(k.id, {enabled: !k.enabled});
      notify.ok(k.enabled ? t('keys.disabled') : t('keys.enabled'));
      load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  // 下游接入地址：客户端才能拿到当前 origin，静态导出阶段为空。
  // 子路径部署时网关也挂在前缀下（反代剥掉前缀再转发），所以要带上 basePath，
  // 否则会把用户引到一个 404 的地址。
  const baseUrl =
    typeof window !== 'undefined' ? `${window.location.origin}${BASE_PATH}` : '';

  // 分组过滤。keys 的量级是「几十到几百」，一次渲染算两遍不值得上 useMemo
  // （那要多写一层依赖数组，还更容易漏依赖）。
  const normalKeys = keys.filter((k) => !k.packet_id);
  const packetKeys = keys.filter((k) => k.packet_id);
  const shownKeys = tab === 'packet' ? packetKeys : normalKeys;

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      {/* 分组：红包一次生成一批、额度零碎，与手工建的混在一起很难看。
          数字直接标在 tab 上，不用切过去才知道另一边有多少个。 */}
      <Tabs value={tab} onValueChange={(v) => setTab(v as 'normal' | 'packet')}>
        <TabsList className="rounded-full">
          <TabsTrigger value="normal" className="rounded-full">
            {t('keys.tabNormal')} · {normalKeys.length}
          </TabsTrigger>
          <TabsTrigger value="packet" className="rounded-full">
            {t('keys.tabPacket')} · {packetKeys.length}
          </TabsTrigger>
        </TabsList>
      </Tabs>

      <PageHeader
        title={t('keys.title')}
        description={t('keys.description')}
        actions={
          <>
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </>
        }
      />

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('metric.name')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colPrefix')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              {/* 版本列紧跟状态：它和状态一样是「这把密钥的属性」，位置与账号页的列序一致 */}
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.realm')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.expiry')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colIpModels')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colUsedTokens')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colLastUsed')}</TableHead>
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {shownKeys.map((k) => {
              const expired = !!k.expires_at && k.expires_at * 1000 < Date.now();
              // 两种额度任一超限都算「超额」——界面上必须与网关的拒绝口径**一致**，
              // 否则会出现「列表显示正常、调用却被 429」，用户会以为是网关坏了。
              const overQuota =
                (!!k.quota && k.used_tokens >= k.quota) ||
                (!!k.quota_credit && k.used_credit >= k.quota_credit);
              return (
                <TableRow key={k.id} className="border-b border-border/40">
                  <TableCell className="pl-4 text-sm font-medium">{k.name}</TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{k.prefix}…</TableCell>
                  <TableCell>
                    {!k.enabled ? (
                      <Badge variant="secondary" className="rounded-full text-muted-foreground">{t('keys.badgeDisabled')}</Badge>
                    ) : expired ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeExpired')}</Badge>
                    ) : overQuota ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeOverQuota')}</Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{t('keys.badgeOk')}</Badge>
                    )}
                  </TableCell>
                  {/* 版本必须排在有效期**前面**，与表头一致（issue #68：这两列的
                      单元格与表头顺序反了，界面上「版本」列显示的是有效期、「有效期」
                      列显示的是版本 —— 用户看到的就是这个错位）。 */}
                  <TableCell>
                    {k.realm === 'global' ? (
                      <Badge variant="secondary" className="rounded-full text-[10px]">{t('realm.global')}</Badge>
                    ) : k.realm === 'cn' ? (
                      <Badge variant="secondary" className="rounded-full text-[10px]">{t('realm.cn')}</Badge>
                    ) : (
                      // 存量密钥：本字段引入前创建的，两版都能调。单独标出来
                      // 而不是默认显示成国内版——那会让人以为它已被限定。
                      <span
                        className="text-[10px] text-amber-600 dark:text-amber-400"
                        title={t('keys.realmUnsetTitle')}
                      >
                        {t('keys.realmUnset')}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.expires_at ? fmtDateTime(k.expires_at) : t('keys.neverExpires')}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.max_ips ? t('keys.ipLimit', {n: k.max_ips}) : t('keys.ipUnlimited')} /{' '}
                    {k.models?.length
                      ? t('keys.modelsCount', {count: k.models.length, n: k.models.length})
                      : t('keys.modelsAll')}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {(() => {
                      const ratio = k.quota ? k.used_tokens / k.quota : 0;
                      const tone = !k.quota
                        ? 'text-muted-foreground'
                        : ratio >= 1
                          ? 'text-red-600 dark:text-red-400 font-medium'
                          : ratio >= 0.8
                            ? 'text-amber-600 dark:text-amber-400 font-medium'
                            : 'text-foreground';
                      // 积分额度设了才显示积分那一行：没设的密钥（绝大多数）
                      // 保持原来的单行 token 展示，不让默认视图变吵。
                      const cRatio = k.quota_credit ? k.used_credit / k.quota_credit : 0;
                      const cTone = cRatio >= 1
                        ? 'text-red-600 dark:text-red-400 font-medium'
                        : cRatio >= 0.8
                          ? 'text-amber-600 dark:text-amber-400 font-medium'
                          : 'text-muted-foreground';
                      return (
                        <span className="flex flex-col">
                          <span className={tone}>
                            {fmtNumber(k.used_tokens)}
                            {k.quota ? ` / ${fmtNumber(k.quota)}` : ''}
                          </span>
                          {!!k.quota_credit && (
                            <span
                              className={`text-[10px] ${cTone}`}
                              title={t('keys.quotaCredit')}
                            >
                              {t('keys.creditUsed', {
                                used: String(k.used_credit),
                                quota: String(k.quota_credit),
                              })}
                            </span>
                          )}
                        </span>
                      );
                    })()}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.last_used_at ? (
                      fmtDateTime(k.last_used_at)
                    ) : (
                      <span className="text-muted-foreground/70">{t('keys.neverUsed')}</span>
                    )}
                  </TableCell>
                  {isAdmin && (
                    <TableCell className="pr-4">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.edit')} onClick={() => openEdit(k)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md"
                          title={k.enabled ? t('keys.disable') : t('keys.enable')}
                          onClick={() => toggle(k)}
                        >
                          {k.enabled ? <Ban className="h-3.5 w-3.5" /> : <CircleCheck className="h-3.5 w-3.5" />}
                        </Button>
                        <ConfirmDialog
                          title={t('keys.resetUsageTitle')}
                          description={t('keys.resetUsageDesc', {name: k.name})}
                          onConfirm={async () => {
                            await keyApi.resetUsage(k.id);
                            notify.ok(t('keys.resetDone'));
                            load();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.resetUsage')}>
                              <RotateCcw className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                        <ConfirmDialog
                          title={t('keys.deleteTitle', {name: k.name})}
                          description={t('keys.deleteDesc')}
                          confirmText={t('keys.delete')}
                          destructive
                          onConfirm={async () => {
                            await keyApi.remove(k.id);
                            notify.ok(t('keys.deleted'));
                            load();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title={t('keys.delete')}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                      </div>
                    </TableCell>
                  )}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        {!keys.length && !loading && (
          <EmptyState
            icon={KeyRound}
            title={t('keys.emptyTitle')}
            description={t('keys.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </EmptyState>
        )}
      </section>

      {/* 新建 / 编辑 */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{editing ? t('keys.editTitle') : t('keys.createTitle')}</DialogTitle>
            <DialogDescription>
              {editing ? t('keys.editDesc') : t('keys.createDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(70vh,560px)]">
            <div className="space-y-4 px-6 pb-2">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.name')}</Label>
                <Input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} placeholder={t('keys.namePlaceholder')} />
              </div>
              {!editing ? (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.expiresInDays')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.expiresDays}
                    onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                  />
                </div>
              ) : (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('keys.expiry')}
                    {editing.expires_at
                      ? t('keys.expiryCurrent', {at: fmtDateTime(editing.expires_at)})
                      : t('keys.expiryNever')}
                  </Label>
                  <div className="flex items-center gap-2">
                    <Select
                      value={form.expiryMode}
                      onValueChange={(v) => setForm({...form, expiryMode: v as FormState['expiryMode']})}
                    >
                      <SelectTrigger className="flex-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="keep">{t('keys.expiryKeep')}</SelectItem>
                        <SelectItem value="days">{t('keys.expiryFromNow')}</SelectItem>
                        <SelectItem value="never">{t('keys.expiryNeverOption')}</SelectItem>
                      </SelectContent>
                    </Select>
                    {form.expiryMode === 'days' && (
                      <Input
                        type="number"
                        min={1}
                        className="w-24"
                        placeholder={t('keys.daysPlaceholder')}
                        value={form.expiresDays}
                        onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                      />
                    )}
                  </div>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.maxIps')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.maxIps}
                    onChange={(e) => setForm({...form, maxIps: e.target.value})}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaTokens')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.quota}
                    onChange={(e) => setForm({...form, quota: e.target.value})}
                  />
                </div>
                <div className="space-y-1.5">
                  {/* 积分额度：与 token 额度各自独立，任一超限即拒绝，两个都留 0 = 不限。
                      step=any 是必要的——上游按倍率扣费，值本身可能是小数（如 0.05），
                      限成整数会让小额预算根本没法设。 */}
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaCredit')}</Label>
                  <Input
                    type="number"
                    min={0}
                    step="any"
                    value={form.quotaCredit}
                    onChange={(e) => setForm({...form, quotaCredit: e.target.value})}
                  />
                  <p className="text-[10px] leading-4 text-muted-foreground">
                    {t('keys.quotaCreditHint')}
                  </p>
                </div>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.ipWhitelist')}</Label>
                <Textarea
                  rows={3}
                  value={form.ipAllowlist}
                  onChange={(e) => setForm({...form, ipAllowlist: e.target.value})}
                  placeholder={'10.0.0.0/8\n1.2.3.4'}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.realmLimit')}</Label>
                {/* 版本归属：**新建时不显示选择器**，直接跟随当前所在版本——
                    在哪个版本的界面里建，就是哪个版本的密钥。
                    用户明确要求过不要让他在这里选：选错了是**静默的**（只有真正
                    调用时才报「仅限某版本」），不如跟随页面、并把结果写清楚。
                    编辑时保留选择器：存量密钥（尤其"不限制"的老密钥）需要能改。 */}
                {editing ? (
                  <>
                    <Select
                      value={form.realm || '__all__'}
                      onValueChange={(v) => setForm({...form, realm: (v === '__all__' ? '' : v) as FormState['realm']})}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="cn">{t('keys.realmCnOnly')}</SelectItem>
                        <SelectItem value="global">{t('keys.realmGlobalOnly')}</SelectItem>
                        {/* 只有存量密钥会停留在这个取值上 */}
                        <SelectItem value="__all__">{t('keys.realmAll')}</SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-[10px] leading-4 text-muted-foreground">
                      {form.realm === ''
                        ? t('keys.realmHintUnlimited')
                        : form.realm === 'global'
                          ? t('keys.realmHintGlobal')
                          : t('keys.realmHintCn')}
                    </p>
                  </>
                ) : (
                  <div className="flex flex-wrap items-center gap-1.5 rounded-lg bg-muted px-3 py-2">
                    <Badge
                      variant="secondary"
                      className={
                        'rounded-full text-[10px] ' +
                        (realm === 'global'
                          ? 'text-blue-600 dark:text-blue-400'
                          : 'text-emerald-600 dark:text-emerald-400')
                      }
                    >
                      {realm === 'global' ? t('realm.global') : t('realm.cn')}
                    </Badge>
                    <span className="text-[10px] leading-4 text-muted-foreground">
                      {t('keys.realmFollowsPage')}
                    </span>
                  </div>
                )}
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.modelWhitelist')}</Label>
                <Input
                  value={form.models}
                  onChange={(e) => {
                    setForm({...form, models: e.target.value});
                    // 改了内容就作废上次结论，避免显示过期的「都对」
                    setUnknownModels(null);
                  }}
                  // 失焦时查一次：不在每次按键时打接口（那是逐字请求），
                  // 但要早于提交——提交时才发现就得重填一遍
                  onBlur={async () => {
                    const names = toLines(form.models);
                    if (!names.length) {
                      setUnknownModels([]);
                      return;
                    }
                    try {
                      const r = await keyApi.checkModels(names, form.realm || '');
                      setUnknownModels(r.checked ? r.unknown : null);
                    } catch {
                      setUnknownModels(null);   // 查不了就不显示，不编造
                    }
                  }}
                  placeholder="glm-5.2, global:gpt-5.4"
                />
                {unknownModels !== null && unknownModels.length > 0 && (
                  <p ref={unknownRef}
                     className="text-[10px] leading-4 text-amber-600 dark:text-amber-400">
                    {t('keys.modelsUnknown', {names: unknownModels.join(t('common.listSeparator'))})}
                  </p>
                )}
                {/* 模型白名单与版本归属是**两道**检查，都要过：
                    版本归属由上面的选项控制（粗粒度，拦跨版本调用），
                    白名单在版本之内再收窄到具体几个模型（细粒度）。
                    写模型名时注意与所选版本一致——带 global: 前缀的是国际版模型。 */}
                <p className="text-[10px] leading-4 text-muted-foreground">
                  {/* 反引号包住的模型名由 RichText 渲染成等宽字体 */}
                  <RichText text={t('keys.modelPrefixNote')} />
                </p>
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setFormOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" onClick={submit} disabled={busy}>
              {editing ? t('common.save') : t('keys.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 一次性展示新密钥 */}
      <Dialog open={!!issued} onOpenChange={(v) => !v && setIssued(null)}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{t('keys.createdTitle')}</DialogTitle>
            <DialogDescription>{t('keys.createdDesc')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 px-6 pb-2">
            {/* min-w-0 必不可少：flex 项默认 min-width:auto，长密钥会把
                复制按钮挤出去（移动端就点不到了） */}
            <div className="flex items-center gap-2 rounded-2xl bg-muted p-3">
              <code className="min-w-0 flex-1 break-all font-mono text-xs">{issued}</code>
              <CopyButton value={issued || ''} size="sm" showLabel label={t('keys.copyKey')} />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-muted-foreground">Base URL</span>
              <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{baseUrl}/v1</code>
              <CopyButton value={`${baseUrl}/v1`} title={t('keys.copyBaseUrl')} />
            </div>
          </div>
          <DialogFooter>
            <Button className="rounded-full" onClick={() => setIssued(null)}>
              {t('keys.savedIt')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
