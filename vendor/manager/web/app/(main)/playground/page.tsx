'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Bot,
  Coins,
  Eraser,
  Info,
  Loader2,
  RefreshCw,
  ThumbsDown,
  ThumbsUp,
  User as UserIcon,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {AiChatInput, type ChatModelOption} from '@/components/ui/ai-chat-input';
import {CopyButton} from '@/components/ui/copy-button';
import {playgroundApi, errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import {fmtCredit} from '@/lib/format';
import {cn} from '@/lib/utils';
import {useAuth} from '@/lib/auth-context';
import {useRealm} from '@/lib/realm-context';

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  /** 本条回答的实测消耗（上游 usage.credit） */
  credit?: number | null;
  tokens?: number;
  /** 是否由流式拼出来的（用于显示光标/反馈按钮） */
  done?: boolean;
  error?: boolean;
}

export default function PlaygroundPage() {
  const {isAdmin} = useAuth();
  const {realm, label: realmName} = useRealm();
  const [models, setModels] = useState<ChatModelOption[]>([]);
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  /** 本次会话累计消耗，实时显示在右下角 */
  const [sessionCredit, setSessionCredit] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinToBottom = useRef(true);

  const loadModels = useCallback(async () => {
    try {
      const r = await playgroundApi.models(realm);
      setModels(r.models || []);
      // 切版本后旧模型多半不在新列表里，直接选第一个，避免发出去被上游拒
      setModel(r.models?.[0]?.id || '');
      setEffort('');
    } catch (e) {
      notify.err(errText(e));
    }
  }, [realm]);

  useEffect(() => {
    loadModels();
  }, [loadModels]);

  // 切换版本 = 换了一套账号池与模型，旧对话留着会造成误解（模型不同、额度不同）
  useEffect(() => {
    setMsgs([]);
    setSessionCredit(0);
  }, [realm]);

  // 自动滚到底部，但用户主动向上翻看时不要抢滚动位置
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !pinToBottom.current) return;
    el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    pinToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput('');
    pinToBottom.current = true;

    const nextMsgs: Msg[] = [...msgs, {role: 'user', content: text}];
    setMsgs([...nextMsgs, {role: 'assistant', content: '', done: false}]);
    setStreaming(true);

    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const res = await fetch('/api/playground/chat', {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        signal: ac.signal,
        body: JSON.stringify({
          model,
          realm,
          reasoning_effort: effort,
          stream: true,
          messages: nextMsgs.map((m) => ({role: m.role, content: m.content})),
        }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const j = await res.json();
          detail = j.detail || j.error?.message || detail;
        } catch {
          /* 响应不是 JSON，保留状态码 */
        }
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {role: 'assistant', content: detail, error: true, done: true};
          return copy;
        });
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error('响应不可读');
      const decoder = new TextDecoder();
      let buf = '';
      let answer = '';
      let credit: number | null = null;
      let tokens = 0;

      // 边读边解析 SSE，逐段追加到气泡上（真实流式体验）
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let idx: number;
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            const delta = obj?.choices?.[0]?.delta?.content;
            if (typeof delta === 'string' && delta) {
              answer += delta;
              setMsgs((cur) => {
                const copy = [...cur];
                copy[copy.length - 1] = {...copy[copy.length - 1], content: answer, done: false};
                return copy;
              });
            }
            if (obj?.usage) {
              if (typeof obj.usage.credit === 'number') credit = obj.usage.credit;
              tokens = (obj.usage.prompt_tokens || 0) + (obj.usage.completion_tokens || 0);
            }
          } catch {
            /* 单个分片解析失败不影响后续 */
          }
        }
      }

      setMsgs((cur) => {
        const copy = [...cur];
        copy[copy.length - 1] = {
          role: 'assistant',
          content: answer || '（模型没有返回内容）',
          credit,
          tokens,
          done: true,
        };
        return copy;
      });
      if (typeof credit === 'number') setSessionCredit((v) => v + credit!);
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setMsgs((cur) => {
          const copy = [...cur];
          const last = copy[copy.length - 1];
          copy[copy.length - 1] = {...last, done: true, content: last.content || '（已停止）'};
          return copy;
        });
      } else {
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {
            role: 'assistant',
            content: errText(e),
            error: true,
            done: true,
          };
          return copy;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, msgs, model, effort, realm, streaming]);

  const stop = () => abortRef.current?.abort();

  const clear = () => {
    if (streaming) return;
    setMsgs([]);
    setSessionCredit(0);
  };

  const currentModel = useMemo(() => models.find((m) => m.id === model), [models, model]);

  return (
    // flex-1 + min-h-0：让页面占满可用高度，对话区随剩余空间自适应。
    // 原先给对话区写死 calc(100dvh-290px)，一旦底部再加说明块就会顶到浮动
    // 底栏下面被遮住（实测遮了 43px）。改用 flex 后不再依赖魔法数字，
    // 任何视口高度都不会重叠。
    <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
      <PageHeader
        title="聊天测试台"
        description={`${realmName}账号试调模型，不需要先创建 API 密钥；真实消耗积分，仅管理员可用`}
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={() => {
                loadModels();
                notify.info('已刷新模型列表');
              }}
            >
              <RefreshCw />
              刷新模型
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={streaming || msgs.length === 0}
              onClick={clear}
            >
              <Eraser />
              清空对话
            </Button>
          </>
        }
      />

      {!isAdmin ? (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4 text-xs">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="space-y-1">
            <div className="font-medium">需要管理员权限</div>
            <div className="text-muted-foreground">
              测试台会真实消耗账号积分，因此只对管理员开放。请让管理员在「设置 → 管理用户」把你的角色调整为
              admin。
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* 对话区。不加卡片底色：消息直接落在页面背景上更清爽，
              也让输入框成为视觉焦点（原先整块 bg-muted 显得很重）。 */}
          <section className="flex min-h-[240px] min-w-0 flex-1 flex-col overflow-hidden">
            <div ref={scrollerRef} onScroll={onScroll} className="scroll-slim min-h-0 flex-1 overflow-y-auto px-1 py-4">
              {msgs.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="max-w-md space-y-2 text-center">
                    <Bot className="mx-auto h-7 w-7 text-muted-foreground/60" />
                    <div className="text-sm font-medium">开始一次试调</div>
                    <p className="text-[11px] leading-5 text-muted-foreground">
                      在下方输入内容并回车即可。请求会经本管理端转发到上游，
                      与下游调用走同一套账号池；回答支持流式输出，
                      每条回答的实际消耗积分会标在气泡下方。
                    </p>
                    {currentModel && (
                      <p className="text-[10px] text-muted-foreground/70">
                        当前模型 <span className="font-mono">{currentModel.id}</span>
                        {currentModel.efforts.length > 0 && `，支持推理档位 ${currentModel.efforts.join(' / ')}`}
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="mx-auto w-full max-w-3xl space-y-4">
                  {msgs.map((m, i) => (
                    <div
                      key={i}
                      className={cn('flex gap-2.5', m.role === 'user' ? 'justify-end' : 'justify-start')}
                    >
                      {m.role === 'assistant' && (
                        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                          <Bot className="h-3.5 w-3.5 text-muted-foreground" />
                        </div>
                      )}
                      <div className={cn('min-w-0 max-w-[82%]', m.role === 'user' && 'flex flex-col items-end')}>
                        <div
                          className={cn(
                            'whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-xs leading-5',
                            m.role === 'user'
                              ? 'bg-foreground text-background'
                              : m.error
                                ? 'border border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400'
                                // 助手气泡用 muted 底：对话区去掉灰色卡片后，
                                // 若仍是 bg-background 就会与页面同色而「隐形」
                                : 'bg-muted',
                          )}
                        >
                          {m.content || <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                          {m.role === 'assistant' && !m.done && m.content && (
                            <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-foreground/50 align-middle" />
                          )}
                        </div>

                        {/* 回答下方：消耗与反馈（对齐参考图里的操作条） */}
                        {m.role === 'assistant' && m.done && !m.error && (
                          <div className="mt-1.5 flex items-center gap-2.5 px-1 text-muted-foreground">
                            <CopyButton
                              value={m.content}
                              label=""
                              title="复制回答"
                              className="h-6 w-6"
                            />
                            <button
                              type="button"
                              title="回答不错"
                              className="transition-colors hover:text-foreground"
                              onClick={() => notify.ok('已记录反馈')}
                            >
                              <ThumbsUp className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              title="回答有问题"
                              className="transition-colors hover:text-foreground"
                              onClick={() => notify.info('已记录反馈', '可到设置页的反馈入口提交详情')}
                            >
                              <ThumbsDown className="h-3 w-3" />
                            </button>
                            {typeof m.credit === 'number' && (
                              <span
                                className="inline-flex items-center gap-1 text-[10px] tabular-nums"
                                title="本次调用的实际扣费（上游 usage.credit）"
                              >
                                <Coins className="h-3 w-3" />
                                {fmtCredit(m.credit)}
                              </span>
                            )}
                            {!!m.tokens && (
                              <span className="text-[10px] tabular-nums text-muted-foreground/70">
                                {m.tokens} tokens
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                      {m.role === 'user' && (
                        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                          <UserIcon className="h-3.5 w-3.5 text-muted-foreground" />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 输入区 + 右下角实时累计消耗。不再画顶部分隔线：对话区已无卡片底色，
                一条横贯的线会显得多余。 */}
            <div className="shrink-0 px-3 pb-3 pt-2.5">
              <div className="mx-auto w-full max-w-3xl">
                <AiChatInput
                  value={input}
                  onChange={setInput}
                  onSend={send}
                  onStop={stop}
                  streaming={streaming}
                  disabled={!isAdmin}
                  models={models}
                  model={model}
                  onModelChange={setModel}
                  effort={effort}
                  onEffortChange={setEffort}
                />
                <div className="mt-1.5 flex items-center justify-between gap-2 px-1">
                  <span className="truncate text-[11px] text-muted-foreground">
                    {models.length > 0
                      ? `共 ${models.length} 个可用模型`
                      : '未取到模型，请检查「模型中心」'}
                  </span>
                  {/* 实时消耗：本次会话累计 */}
                  <Badge
                    variant="secondary"
                    className="shrink-0 rounded-full text-[10px] tabular-nums"
                    title="本次会话累计消耗的积分（每条回答来自上游 usage.credit）"
                  >
                    <Coins className="mr-1 h-3 w-3" />
                    本次消耗 {fmtCredit(sessionCredit)}
                  </Badge>
                </div>
              </div>
            </div>
          </section>

          {/*
            说明文字：居中的一段小字，限制宽度让它自然折行。
            不做成通栏宽度的卡片——那样在宽屏下就是「一条横」，
            既占地方又不好读；居中窄栏反而更像一句注脚。
            字号 11px / 行高 20px：中文在这个组合下不挤，也不至于淡到看不清。
          */}
          <p className="mx-auto max-w-lg shrink-0 text-center text-[11px] leading-5 text-muted-foreground">
            测试台仅用于调试（走管理端登录态，不经密钥与 IP 管控）
            <span className="mx-1.5 text-muted-foreground/40">·</span>
            正式接入请用「API 密钥」页签发的 wbk_ 密钥
            <span className="mx-1.5 text-muted-foreground/40">·</span>
            不支持的推理档位上游会自动降级
          </p>
        </>
      )}
    </div>
  );
}
