import {useState, useEffect, useRef, useCallback} from 'react';
import {FloatingDock} from '@/components/ui/floating-dock';
import packageJson from '../../../package.json';
import {
  MessageCircleIcon,
  BarChart3,
  Users,
  ClipboardList,
  KeyRound,
  Boxes,
  MessageSquare,
  ScrollText,
  TrendingUp,
  ShieldCheck,
  Settings,
  PlusCircle,
  User,
  LogOut as LogOutIcon,
  Link2,
  FolderGit2,
  ChevronRight,
} from 'lucide-react';
import {useThemeUtils} from '@/hooks/use-theme-utils';
import {useAuth} from '@/lib/auth-context';
import {accountApi, systemApi} from '@/lib/api';
import {notify} from '@/lib/toast';
import {CountingNumber} from '@/components/animate-ui/text/counting-number';
import {Button} from '@/components/ui/button';
import Link from 'next/link';
import {Badge} from '@/components/ui/badge';
import {Separator} from '@/components/ui/separator';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from '@/components/animate-ui/radix/dialog';
import {Avatar, AvatarFallback} from '@/components/ui/avatar';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {AddAccountDialog} from '@/components/common/accounts/AddAccountDialog';

const IconOptions = {
  className: 'h-4 w-4',
} as const;

// v2：坐标语义由「左边缘」改为「水平中心」，旧版本存储的位置不再兼容
const DOCK_STORAGE_KEY = 'workbuddy-manager:dock-position-v2';
const DOCK_TIP_STORAGE_KEY = 'workbuddy-manager:dock-tip-dismissed';
const DOCK_MARGIN = 16;
const DOCK_LONG_PRESS_MS = 180;
const DOCK_CLICK_SUPPRESS_MS = 220;
const DOCK_INTERACTIVE_SELECTOR = 'a,button,input,textarea,select,[role="button"],[data-dock-no-drag="true"]';

type DockViewport = 'desktop' | 'mobile';

type DockPosition = {
  x: number;
  y: number;
};

type StoredDockPosition = DockPosition & {
  viewportWidth?: number;
  viewportHeight?: number;
};

type DockPositions = Partial<Record<DockViewport, StoredDockPosition>>;

const SystemTheme = {
  LIGHT: 'light',
  DARK: 'dark',
} as const;

export function ManagementBar() {
  const themeUtils = useThemeUtils();
  const {me, isAdmin, logout} = useAuth();
  const [mounted, setMounted] = useState(false);
  const [dockViewport, setDockViewport] = useState<DockViewport>('desktop');
  const [profileOpen, setProfileOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [dockPosition, setDockPosition] = useState<DockPosition | null>(null);
  const [showDockTip, setShowDockTip] = useState(false);
  const [dockTipStep, setDockTipStep] = useState(0);
  /** 受管账号数量（真实数据，供个人信息面板展示） */
  const [accountCount, setAccountCount] = useState<number | null>(null);
  const dockRef = useRef<HTMLDivElement>(null);
  const dockViewportRef = useRef<DockViewport>('desktop');
  const dragOffsetRef = useRef({x: 0, y: 0});
  const dockPressTimerRef = useRef<number | null>(null);
  const pressStartRef = useRef({x: 0, y: 0});
  const isDraggingRef = useRef(false);
  const suppressClickUntilRef = useRef(0);

  const getViewport = useCallback((): DockViewport => (window.innerWidth >= 768 ? 'desktop' : 'mobile'), []);

  const readDockPositions = useCallback((): DockPositions => {
    if (typeof window === 'undefined') return {};

    try {
      const raw = window.localStorage.getItem(DOCK_STORAGE_KEY);
      return raw ? JSON.parse(raw) as DockPositions : {};
    } catch {
      return {};
    }
  }, []);

  const writeDockPosition = useCallback((viewport: DockViewport, position: DockPosition) => {
    if (typeof window === 'undefined') return;

    const nextPositions = {
      ...readDockPositions(),
      [viewport]: {
        ...position,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
      },
    };

    window.localStorage.setItem(DOCK_STORAGE_KEY, JSON.stringify(nextPositions));
  }, [readDockPositions]);

  const getDockRect = useCallback(() => {
    const rect = dockRef.current?.getBoundingClientRect();
    return {
      width: rect?.width ?? (dockViewportRef.current === 'desktop' ? 620 : 52),
      height: rect?.height ?? (dockViewportRef.current === 'desktop' ? 88 : 52),
    };
  }, []);

  /**
   * 这里的坐标是「底栏水平中心」而非左边缘。
   * 容器通过 transform: translateX(-50%) 以中心对齐，
   * 这样鼠标悬停导致图标放大、底栏总宽变化时，会向两侧对称扩展，
   * 视觉上不会发生位移。
   */
  const clampDockPosition = useCallback((position: DockPosition): DockPosition => {
    if (typeof window === 'undefined') return position;

    const {width, height} = getDockRect();
    const halfW = width / 2;
    const minX = DOCK_MARGIN + halfW;
    const maxX = Math.max(minX, window.innerWidth - DOCK_MARGIN - halfW);
    const maxY = Math.max(DOCK_MARGIN, window.innerHeight - height - DOCK_MARGIN);

    return {
      x: Math.min(Math.max(position.x, minX), maxX),
      y: Math.min(Math.max(position.y, DOCK_MARGIN), maxY),
    };
  }, [getDockRect]);

  const getDefaultDockPosition = useCallback((viewport: DockViewport): DockPosition => {
    if (typeof window === 'undefined') return {x: DOCK_MARGIN, y: DOCK_MARGIN};

    const {width, height} = getDockRect();
    const basePosition = viewport === 'desktop' ?
      {
        x: window.innerWidth / 2,
        y: window.innerHeight - height - DOCK_MARGIN,
      } :
      {
        x: window.innerWidth - DOCK_MARGIN - width / 2,
        y: window.innerHeight - height - DOCK_MARGIN,
      };

    return clampDockPosition(basePosition);
  }, [clampDockPosition, getDockRect]);

  const getScaledDockPosition = useCallback((position: StoredDockPosition): DockPosition => {
    if (typeof window === 'undefined' || !position.viewportWidth || !position.viewportHeight) {
      return clampDockPosition(position);
    }

    const {width, height} = getDockRect();
    const halfW = width / 2;
    const oldMinX = DOCK_MARGIN + halfW;
    const oldMaxX = Math.max(oldMinX, position.viewportWidth - DOCK_MARGIN - halfW);
    const oldMaxY = Math.max(DOCK_MARGIN, position.viewportHeight - height - DOCK_MARGIN);
    const nextMinX = DOCK_MARGIN + halfW;
    const nextMaxX = Math.max(nextMinX, window.innerWidth - DOCK_MARGIN - halfW);
    const nextMaxY = Math.max(DOCK_MARGIN, window.innerHeight - height - DOCK_MARGIN);
    const xRatio = oldMaxX === oldMinX ? 0.5 : (position.x - oldMinX) / (oldMaxX - oldMinX);
    const yRatio = oldMaxY === DOCK_MARGIN ? 0 : (position.y - DOCK_MARGIN) / (oldMaxY - DOCK_MARGIN);

    return clampDockPosition({
      x: nextMinX + xRatio * (nextMaxX - nextMinX),
      y: DOCK_MARGIN + yRatio * (nextMaxY - DOCK_MARGIN),
    });
  }, [clampDockPosition, getDockRect]);

  const syncDockPosition = useCallback((nextViewport?: DockViewport) => {
    if (typeof window === 'undefined') return;

    const viewport = nextViewport ?? getViewport();
    dockViewportRef.current = viewport;
    setDockViewport(viewport);

    const savedPosition = readDockPositions()[viewport];
    const nextPosition = savedPosition ? getScaledDockPosition(savedPosition) : getDefaultDockPosition(viewport);
    setDockPosition(nextPosition);
  }, [getDefaultDockPosition, getScaledDockPosition, getViewport, readDockPositions]);

  useEffect(() => {
    setMounted(true);
  }, []);

  // 拉取受管账号数量；账号页增删后通过自定义事件刷新
  useEffect(() => {
    let alive = true;
    const fetchCount = async () => {
      try {
        const data = await accountApi.list();
        if (alive) setAccountCount(data.total);
      } catch {
        if (alive) setAccountCount(null);
      }
    };
    fetchCount();
    window.addEventListener('workbuddy-manager:accounts-changed', fetchCount);
    return () => {
      alive = false;
      window.removeEventListener('workbuddy-manager:accounts-changed', fetchCount);
    };
  }, []);

  // 每个浏览器会话检测一次新版本，有更新则弹出提醒（避免打扰不重复提示）
  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;
    const KEY = 'workbuddy-manager:update-notified';
    if (window.sessionStorage.getItem(KEY) === '1') return;
    window.sessionStorage.setItem(KEY, '1');

    (async () => {
      try {
        const c = await systemApi.checkUpdate();
        if (!c.has_any) return;
        const parts: string[] = [];
        if (c.manager.has_update) parts.push(`管理端 ${c.manager.latest}`);
        if (c.upstream.has_update) parts.push(`上游 ${c.upstream.latest}`);
        notify.warn('发现新版本可更新', `${parts.join(' · ')}　到「设置 → 系统更新」一键升级`);
      } catch {
        /* 检测失败静默：不打扰用户（如服务器访问 GitHub 受限） */
      }
    })();
  }, [mounted]);

  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;

    const dismissed = window.localStorage.getItem(DOCK_TIP_STORAGE_KEY) === 'true';
    if (!dismissed) {
      setShowDockTip(true);
    }
  }, [mounted]);

  useEffect(() => {
    if (!mounted) return;

    const frameId = window.requestAnimationFrame(() => {
      syncDockPosition();
    });

    const handleResize = () => {
      window.requestAnimationFrame(() => {
        const viewport = getViewport();
        const savedPosition = readDockPositions()[viewport];

        dockViewportRef.current = viewport;
        setDockViewport(viewport);
        setDockPosition(savedPosition ? getScaledDockPosition(savedPosition) : getDefaultDockPosition(viewport));
      });
    };

    window.addEventListener('resize', handleResize);

    return () => {
      window.cancelAnimationFrame(frameId);
      window.removeEventListener('resize', handleResize);
    };
  }, [getDefaultDockPosition, getScaledDockPosition, getViewport, mounted, readDockPositions, syncDockPosition]);

  useEffect(() => {
    const openAdd = () => {
      setProfileOpen(false);
      setAddOpen(true);
    };
    window.addEventListener('workbuddy-manager:open-add-account', openAdd);
    return () => {
      window.removeEventListener('workbuddy-manager:open-add-account', openAdd);
    };
  }, []);

  const handleLogout = () => {
    logout();
  };

  const handleDismissDockTip = useCallback(() => {
    setShowDockTip(false);
    setDockTipStep(0);
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(DOCK_TIP_STORAGE_KEY, 'true');
    }
  }, []);

  const dockTipSteps = dockViewport === 'mobile' ?
    [
      '菜单栏已调整至此处，点击即可展开。',
      '长按菜单栏空白区域可自定义拖动位置。',
      '展开后，倒数第二个按钮用于快速添加腾讯账号。',
      '展开后，倒数第一个按钮用于访问个人设置与账户信息。',
    ] :
    [
      '菜单栏已调整至此处，长按空白区域可自定义拖动位置。',
      '右侧第二个按钮用于快速扫码添加腾讯账号。',
      '右侧第一个按钮用于访问个人设置与账户信息。',
    ];

  const handleNextDockTip = useCallback(() => {
    setDockTipStep((current) => {
      if (current >= dockTipSteps.length - 1) {
        handleDismissDockTip();
        return current;
      }
      return current + 1;
    });
  }, [dockTipSteps.length, handleDismissDockTip]);

  const clearDockPressTimer = useCallback(() => {
    if (dockPressTimerRef.current !== null) {
      window.clearTimeout(dockPressTimerRef.current);
      dockPressTimerRef.current = null;
    }
  }, []);

  const beginDockDrag = useCallback((clientX: number, clientY: number) => {
    if (!dockPosition || typeof window === 'undefined') return;

    const viewport = getViewport();
    dockViewportRef.current = viewport;
    // dockPosition.x 是底栏中心点，因此偏移量相对中心计算
    dragOffsetRef.current = {
      x: clientX - dockPosition.x,
      y: clientY - dockPosition.y,
    };
    isDraggingRef.current = true;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const nextPosition = clampDockPosition({
        x: moveEvent.clientX - dragOffsetRef.current.x,
        y: moveEvent.clientY - dragOffsetRef.current.y,
      });

      setDockPosition(nextPosition);
    };

    const handlePointerUp = (upEvent: PointerEvent) => {
      const nextPosition = clampDockPosition({
        x: upEvent.clientX - dragOffsetRef.current.x,
        y: upEvent.clientY - dragOffsetRef.current.y,
      });

      setDockPosition(nextPosition);
      writeDockPosition(dockViewportRef.current, nextPosition);
      isDraggingRef.current = false;
      suppressClickUntilRef.current = Date.now() + DOCK_CLICK_SUPPRESS_MS;
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      window.removeEventListener('pointercancel', handlePointerUp);
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);
    window.addEventListener('pointercancel', handlePointerUp);
  }, [clampDockPosition, dockPosition, getViewport, writeDockPosition]);

  const handleDockPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (!dockPosition || event.button !== 0) return;
    if ((event.target as HTMLElement).closest(DOCK_INTERACTIVE_SELECTOR)) return;

    pressStartRef.current = {x: event.clientX, y: event.clientY};
    clearDockPressTimer();
    dockPressTimerRef.current = window.setTimeout(() => {
      beginDockDrag(pressStartRef.current.x, pressStartRef.current.y);
      dockPressTimerRef.current = null;
    }, DOCK_LONG_PRESS_MS);
  }, [beginDockDrag, clearDockPressTimer, dockPosition]);

  const handleDockPointerEnd = useCallback(() => {
    if (!isDraggingRef.current) {
      clearDockPressTimer();
    }
  }, [clearDockPressTimer]);

  const handleDockClickCapture = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    if (Date.now() > suppressClickUntilRef.current) return;

    event.preventDefault();
    event.stopPropagation();
  }, []);

  const dockItems = [
    {
      title: '仪表盘',
      icon: <BarChart3 {...IconOptions} />,
      href: '/dashboard',
    },
    {
      title: '账号',
      icon: <Users {...IconOptions} />,
      href: '/accounts',
    },
    {
      title: '任务',
      icon: <ClipboardList {...IconOptions} />,
      href: '/tasks',
    },
    {
      title: '密钥',
      icon: <KeyRound {...IconOptions} />,
      href: '/keys',
    },
    {
      title: '模型',
      icon: <Boxes {...IconOptions} />,
      href: '/models',
    },
    {
      title: '测试台',
      icon: <MessageSquare {...IconOptions} />,
      href: '/playground',
    },
    {
      title: 'divider',
      icon: <div />,
    },
    {
      title: '用量',
      icon: <TrendingUp {...IconOptions} />,
      href: '/stats',
    },
    {
      title: '日志',
      icon: <ScrollText {...IconOptions} />,
      href: '/logs',
    },
    {
      title: '安全',
      icon: <ShieldCheck {...IconOptions} />,
      href: '/security',
    },
    {
      title: '设置',
      icon: <Settings {...IconOptions} />,
      href: '/settings',
    },
    {
      title: '快速添加',
      icon: <PlusCircle {...IconOptions} />,
      customComponent: (
        <div
          onClick={() => {
            if (isAdmin) setAddOpen(true);
          }}
          className="w-full h-full flex items-center justify-center cursor-pointer rounded transition-colors"
        >
          <PlusCircle className="h-4 w-4" />
        </div>
      ),
    },
    {
      title: '个人信息',
      icon: <User {...IconOptions} />,
      customComponent: (
        <>
          <Dialog open={profileOpen} onOpenChange={setProfileOpen}>
            <DialogTrigger asChild>
              <div className="w-full h-full flex items-center justify-center cursor-pointer rounded transition-colors">
                <User className="h-4 w-4" />
              </div>
            </DialogTrigger>
            <DialogContent
              showCloseButton
              className="max-w-[520px]"
              onOpenAutoFocus={(event) => {
                // 阻止自动聚焦到「退出登录」，否则按钮会出现焦点圈，易被误触
                event.preventDefault();
              }}
            >
              <DialogHeader>
                <DialogTitle>个人信息</DialogTitle>
                <DialogDescription>
                  管理账户信息、主题偏好与登录会话 · 点击空白处或按 Esc 关闭
                </DialogDescription>
              </DialogHeader>
              <DialogBody className="max-h-[min(72vh,560px)]">
                <div className="space-y-5 px-5 pb-4">
                  {me && (
                    <>
                      <div className="space-y-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex min-w-0 items-center gap-3">
                            <Avatar className="size-12 rounded-full">
                              <AvatarFallback className="bg-muted text-sm font-semibold text-foreground">
                                {me.username?.slice(0, 2).toUpperCase() || 'U'}
                              </AvatarFallback>
                            </Avatar>
                            <div className="min-w-0">
                              <div className="truncate text-[15px] font-semibold text-foreground">
                                {me.username}
                              </div>
                              <div className="truncate text-xs text-muted-foreground">
                                WorkBuddy Manager
                              </div>
                              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                                <Badge variant="secondary" className="h-5 rounded-full px-2 text-[10px]">
                                  {me.role === 'admin' ? '管理员' : '只读用户'}
                                </Badge>
                              </div>
                            </div>
                          </div>
                          <ConfirmDialog
                            title="确认退出登录？"
                            description="退出后需要重新输入用户名与密码才能进入管理端。"
                            confirmText="退出登录"
                            destructive
                            onConfirm={handleLogout}
                            trigger={
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-8 shrink-0 rounded-full text-muted-foreground hover:text-red-600"
                              >
                                <LogOutIcon className="size-3.5" />
                                退出登录
                              </Button>
                            }
                          />
                        </div>

                        <Separator />

                        <div className="space-y-2">
                          <div className="text-[11px] font-medium text-muted-foreground">账号池概览</div>
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2">
                              <Users className="size-3.5 text-foreground/60" />
                              <span className="text-xs font-medium text-foreground">受管账号</span>
                              <span className="text-xs font-semibold tabular-nums text-foreground">
                                {accountCount === null ? (
                                  '—'
                                ) : (
                                  // 不用 inView：弹窗以缩放动画挂载时，
                                  // useInView(once) 可能判定为不可见而停在初始值 0
                                  <CountingNumber
                                    number={accountCount}
                                    fromNumber={0}
                                    transition={{stiffness: 200, damping: 25}}
                                  />
                                )}
                              </span>
                            </div>
                          </div>
                        </div>

                        {mounted && (
                          <div className="space-y-2">
                            <div className="text-[11px] font-medium text-muted-foreground">系统设置</div>
                            <div className="flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                onClick={themeUtils.toggle}
                                className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                              >
                                <div className="flex items-center gap-2">
                                  {themeUtils.getIcon('size-3.5 text-foreground/60')}
                                  <span className="text-xs font-medium text-foreground">
                                    {themeUtils.getSystemTheme() === SystemTheme.LIGHT ? '浅色模式' : '深色模式'}
                                  </span>
                                </div>
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    </>
                  )}

                  <div className="space-y-2">
                    <div className="text-[11px] font-medium text-muted-foreground">快速链接</div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        href="https://github.com/Sliverkiss/workbuddy2api"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <FolderGit2 className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">workbuddy2api</span>
                        </div>
                      </Link>
                      <Link
                        href="https://github.com/linux-do/cdk"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <Link2 className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">UI 参考</span>
                        </div>
                      </Link>
                      <Link
                        href="/settings"
                        className="inline-flex items-center gap-2 rounded-full bg-muted px-3 py-2 transition-colors hover:bg-muted/80"
                      >
                        <div className="flex items-center gap-2">
                          <MessageCircleIcon className="size-3.5 text-foreground/60" />
                          <span className="text-xs font-medium text-foreground">帮助</span>
                        </div>
                      </Link>
                    </div>
                  </div>

                  <Separator />

                  <div className="space-y-2">
                    <div className="text-xs font-medium">关于 WorkBuddy Manager</div>
                    <div className="space-y-1.5">
                      <div className="text-[11px] font-light text-muted-foreground">
                        Version {packageJson.version}, Build At {packageJson.buildDate}
                      </div>
                      <div className="text-[11px] font-light leading-5 text-muted-foreground">
                        WorkBuddy Manager 是为 workbuddy2api 打造的账号池管理与 OpenAI 兼容反代网关，支持多账号扫码纳管、自动签到、密钥分发、IP 管控与用量统计。
                      </div>
                    </div>
                  </div>
                </div>
              </DialogBody>
            </DialogContent>
          </Dialog>
        </>
      ),
    },
  ];

  return (
    <>
      <div
        ref={dockRef}
        className="fixed z-40 select-none touch-none"
        onPointerDown={handleDockPointerDown}
        onPointerUp={handleDockPointerEnd}
        onPointerCancel={handleDockPointerEnd}
        onClickCapture={handleDockClickCapture}
        style={
          dockPosition ?
            {left: dockPosition.x, top: dockPosition.y, transform: 'translateX(-50%)'} :
            {visibility: 'hidden'}
        }
      >
        {showDockTip && (
          <div
            data-dock-no-drag="true"
            className="pointer-events-auto absolute bottom-full right-0 mb-2 w-[min(15rem,calc(100vw-2.5rem))] rounded-2xl border border-border/60 bg-background/95 px-2.5 py-2 text-left shadow-[0_18px_40px_rgba(15,23,42,0.12)] ring-1 ring-black/[0.03] backdrop-blur-sm md:left-1/2 md:right-auto md:mb-2.5 md:w-[17rem] md:-translate-x-1/2 md:px-3 dark:bg-background dark:shadow-[0_18px_40px_rgba(0,0,0,0.35)] dark:ring-white/[0.04]"
            onPointerDown={(event) => event.stopPropagation()}
            onPointerUp={(event) => event.stopPropagation()}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="space-y-1.5">
              <div className="text-[10px] font-medium leading-4 text-foreground md:text-[11px]">
                菜单栏引导
              </div>
              <p className="text-[10px] leading-4 text-muted-foreground md:text-[11px]">
                {dockTipSteps[dockTipStep]}
              </p>
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-1.5">
                  {dockTipSteps.map((_, index) => (
                    <span
                      key={index}
                      className={`h-1.5 rounded-full transition-all ${index === dockTipStep ? 'w-4 bg-foreground/75' : 'w-1.5 bg-muted-foreground/25'}`}
                    />
                  ))}
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-5 rounded-full px-1 text-[10px] text-muted-foreground md:h-6 md:px-1.5 md:text-[11px]"
                  onClick={handleNextDockTip}
                >
                  <ChevronRight className="size-3 md:size-3.5" />
                </Button>
              </div>
            </div>
          </div>
        )}
        <FloatingDock
          items={dockItems}
          desktopClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-16 pb-3 px-4 gap-2"
          mobileButtonClassName="bg-background/70 backdrop-blur-md border border-border/40 shadow-lg shadow-black/10 dark:shadow-white/5 h-12 w-12"
        />
      </div>

      <AddAccountDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        onSuccess={() => {
          /* 账号页会在打开时自行刷新 */
        }}
      />
    </>
  );
}
