'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {notify} from '@/lib/toast';
import {Loader2, CheckCircle2, AlertTriangle, ExternalLink} from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {accountApi, errText} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton, ShareButton} from '@/components/ui/copy-button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
} from '@/components/animate-ui/radix/dialog';

type Phase = 'loading' | 'waiting' | 'success' | 'error';

/**
 * 国际版可选地区（与后端 INTERNATIONAL_REGIONS 保持一致）。
 * 取自国际版官网的短名单；不预选，因为地区属于账号归属信息。
 */
const INTERNATIONAL_REGIONS = [
  {code: 'HK', label: '中国香港'},
  {code: 'MO', label: '中国澳门'},
  {code: 'SG', label: '新加坡'},
  {code: 'TH', label: '泰国'},
  {code: 'PH', label: '菲律宾'},
  {code: 'MY', label: '马来西亚'},
  {code: 'ID', label: '印度尼西亚'},
] as const;

export function AddAccountDialog({
  open,
  onOpenChange,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSuccess?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>('loading');
  /** 版本跟随全站切换：切到国际版时扫码走国际版端点，并需要选地区 */
  const {realm, label: realmName} = useRealm();
  /** 国际版地区代码（如 HK）。不预选：地区属于账号归属信息，交由用户决定 */
  const [region, setRegion] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const stateRef = useRef('');
  const timerRef = useRef<number | null>(null);
  /** 上一次 poll 是否还在飞：单次超过 2 秒时避免请求叠加 */
  const pollingRef = useRef(false);

  const stopPoll = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    pollingRef.current = false;
  }, []);

  const start = useCallback(async () => {
    stopPoll();
    setPhase('loading');
    setMessage('正在向腾讯申请授权链接…');
    setAuthUrl('');
    try {
      const data = await accountApi.start(realm);
      stateRef.current = data.state;
      setAuthUrl(data.authUrl);
      setPhase('waiting');
      setMessage('等待手机扫码确认…');

      timerRef.current = window.setInterval(async () => {
        if (pollingRef.current) return;  // 上一次还没回来，跳过本轮
        pollingRef.current = true;
        try {
          const res = await accountApi.poll(stateRef.current, realm, region || undefined);
          if (res.status === 'success') {
            stopPoll();
            setPhase('success');
            setMessage(`账号「${res.nickname || res.uid}」授权成功${res.updated ? '（已更新）' : ''}`);
            notify.ok(
              `账号「${res.nickname || res.uid}」授权成功`,
              res.realm === 'global'
                ? '国际版账号已加入账号池（国际版无签到，积分来自一次性 trial）'
                : res.updated
                  ? '已更新该账号的登录令牌，正在自动应用'
                  : '已自动完成签到，正在加入账号池',
            );
            window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
            onSuccess?.();
            window.setTimeout(() => onOpenChange(false), 1600);
          } else if (res.status === 'expired' || res.status === 'invalid') {
            stopPoll();
            setPhase('error');
            setMessage('二维码已失效，请关闭后重试');
          }
        } catch {
          /* 忽略单次轮询错误，等待下次 */
        } finally {
          pollingRef.current = false;
        }
      }, 2000);
    } catch (e) {
      setPhase('error');
      setMessage(errText(e));
    }
  }, [onOpenChange, onSuccess, stopPoll, realm, region]);

  useEffect(() => {
    if (open) {
      start();
    } else {
      stopPoll();
    }
    return stopPoll;
    // 版本或地区变化时重新申请：扫码码是绑定端点的，旧码不能跨版本用
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, realm, region]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>添加腾讯账号 · {realmName}</DialogTitle>
          <DialogDescription>
            {realm === 'global'
              ? '扫码授权国际版账号（workbuddy.ai）。新号需先选地区完成注册，否则聊天会报 14017'
              : '使用微信 / QQ 扫码完成授权，成功后自动签到并纳管'}
          </DialogDescription>
        </DialogHeader>

        <div className="flex w-full min-w-0 flex-col items-center gap-4 px-6 pb-6">
          {/* 国际版必须先完成地区注册，否则聊天报 14017。
              放在二维码之前：地区一变就要重新申请授权码，先选好再扫省得白扫。 */}
          {realm === 'global' && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">地区（用于国际版注册）</div>
              <Select value={region} onValueChange={setRegion}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs">
                  <SelectValue placeholder="请选择地区（不替你默认，避免归属填错）" />
                </SelectTrigger>
                <SelectContent>
                  {INTERNATIONAL_REGIONS.map((r) => (
                    <SelectItem key={r.code} value={r.code} className="text-xs">
                      {r.label}（{r.code}）
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!region && (
                <p className="text-[10px] leading-4 text-muted-foreground">
                  不选也可扫码，但账号会停留在「未注册地区」状态，聊天报 14017 时需回来重选。
                </p>
              )}
            </div>
          )}

          {/* 固定尺寸，避免 loading/waiting/error 各阶段弹窗高度跳动 */}
          <div className="grid h-[212px] w-[212px] shrink-0 place-items-center overflow-hidden rounded-2xl bg-white p-3 ring-1 ring-black/5">
            {phase === 'loading' && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
            {phase === 'error' && <AlertTriangle className="h-7 w-7 text-amber-500" />}
            {(phase === 'waiting' || phase === 'success') && authUrl && (
              <QRCodeSVG value={authUrl} size={188} level="M" />
            )}
          </div>

          {authUrl && (
            <div className="w-full space-y-2">
              {/* 链接本身可点开；旁边给复制与分享，便于把授权链接发给朋友 */}
              <div className="flex w-full items-center gap-1.5 rounded-full bg-muted px-3 py-1.5">
                <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
                <a
                  href={authUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={authUrl}
                  className="min-w-0 flex-1 truncate text-[11px] text-blue-500 hover:underline"
                >
                  {authUrl}
                </a>
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                <CopyButton
                  value={authUrl}
                  size="sm"
                  showLabel
                  label="复制链接"
                  variant="outline"
                  className="rounded-full"
                />
                <ShareButton
                  title="添加腾讯账号"
                  text="打开这个链接完成扫码授权，之后会自动签到并纳入账号池"
                  url={authUrl}
                />
              </div>
            </div>
          )}

          <div
            className={
              'flex items-center gap-2 px-2 text-xs ' +
              (phase === 'success' ?
                'text-emerald-500' :
                phase === 'error' ?
                  'text-red-500' :
                  'text-muted-foreground')
            }
          >
            {phase === 'waiting' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
            {phase === 'success' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}
            <span className="text-center">{message}</span>
          </div>

          <div className="flex w-full gap-2">
            <Button variant="outline" className="flex-1 rounded-full" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            {phase === 'error' && (
              <Button className="flex-1 rounded-full" onClick={start}>
                重新获取
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
