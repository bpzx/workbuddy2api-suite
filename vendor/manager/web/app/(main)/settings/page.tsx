'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {
  Settings as SettingsIcon,
  RefreshCw,
  Plus,
  Trash2,
  Save,
  Users,
  Server,
  Shuffle,
  Info,
  TriangleAlert,
  ChevronDown,
  RotateCcw,
  PlugZap,
  Loader2,
  DownloadCloud,
  FileText,
} from 'lucide-react';
import {notify} from '@/lib/toast';
import {settingsApi, upstreamApi, errText} from '@/lib/api';
import type {ModelInfo, ModelSource, UpstreamConfig, UserItem} from '@/lib/types';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {UpdatePanel} from '@/components/common/settings/UpdatePanel';
import {ChangelogPanel} from '@/components/common/settings/ChangelogPanel';
import {CopyButton} from '@/components/ui/copy-button';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/* ─────────────────────────────────────────────────────────
 * 可视化配置字段定义
 * 每个字段对应 workbuddy2api config.json 中的一个键，
 * 这里给出中文名称、白话说明与安全取值范围。
 * ───────────────────────────────────────────────────────── */

interface BoolField {
  key: string;
  kind: 'bool';
  label: string;
  desc: string;
  def: boolean;
}

interface NumField {
  key: string;
  kind: 'num';
  label: string;
  desc: string;
  unit?: string;
  min: number;
  max: number;
  /** 步进，缺省 1；小数参数用 0.1 */
  step?: number;
  def: number;
}

interface HoursField {
  key: string;
  /** 时刻数组：上游是 []int，如 [9, 21] 表示每天 9 点与 21 点执行 */
  kind: 'hours';
  label: string;
  desc: string;
  def: number[];
}

interface DurationField {
  key: string;
  /** 时长字符串：上游接受 30s / 10m / 2h 等 */
  kind: 'duration';
  label: string;
  desc: string;
  def: string;
}

interface SelectField {
  key: string;
  /** 枚举字符串：上游只接受给定取值 */
  kind: 'select';
  label: string;
  desc: string;
  /** 危险项：填错会导致上游启动失败，界面上给显式警示 */
  caution?: string;
  options: {value: string; label: string}[];
  def: string;
}

interface TextField {
  key: string;
  /** 自由文本（文件路径之类），不做格式假设，仅禁止换行/控制字符 */
  kind: 'text';
  label: string;
  desc: string;
  caution?: string;
  placeholder?: string;
  def: string;
}

type Field =
  | BoolField
  | NumField
  | HoursField
  | DurationField
  | SelectField
  | TextField;

/** 时长格式校验：数字 + 单位（s/m/h/d） */
const DURATION_RE = /^\d+\s*(s|m|h|d)$/i;

/** 把时刻数组格式化为可读文本，如 [9,21] -> "9, 21" */
function hoursToText(v: unknown): string {
  if (Array.isArray(v)) {
    return v
      .filter((x): x is number => typeof x === 'number')
      .sort((a, b) => a - b)
      .join(', ');
  }
  return '';
}

/** 解析用户输入的时刻列表：返回 {ok, hours?, error?} */
function parseHours(text: string): {ok: boolean; hours: number[]; error?: string} {
  const parts = text.split(/[,，\s]+/).filter(Boolean);
  if (!parts.length) return {ok: false, hours: [], error: '请至少填一个时刻'};
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n < 0 || n > 23) {
      return {ok: false, hours: [], error: `「${p}」不是 0-23 的整点`};
    }
    if (!out.includes(n)) out.push(n);
  }
  return {ok: true, hours: out.sort((a, b) => a - b)};
}

const SCHEDULE_FIELDS: Field[] = [
  {
    key: 'checkin_enabled',
    kind: 'bool',
    label: '自动签到',
    desc: '每天自动领取免费额度；签到时的余额查询还能解冻被冷却的账号',
    def: true,
  },
  {
    key: 'checkin_hours',
    kind: 'hours',
    label: '签到时刻',
    desc: '在哪些整点执行签到（0-23，可多个）。签到时同时刷新余额并解冻冷却账号',
    def: [9, 21],
  },
  {
    key: 'travel_enabled',
    kind: 'bool',
    label: '猫猫旅行',
    desc: '自动推进「猫猫旅行」：领养 / 派出 / 领取到站奖励，可获得积分',
    def: true,
  },
  {
    key: 'travel_hours',
    kind: 'hours',
    label: '旅行时刻',
    desc: '在哪些整点推进旅行（0-23，可多个）。默认两趟闭环：早上领奖并派出，晚上领当日奖励',
    def: [9, 21],
  },
  {
    key: 'activity_enabled',
    kind: 'bool',
    label: '活跃上报',
    desc: '每日上报一次对话活跃，点亮连续登录并解锁领养前置任务（领猫需要）',
    def: true,
  },
  {
    key: 'activity_hours',
    kind: 'hours',
    label: '上报时刻',
    desc: '在哪些整点上报（0-23，可多个）。每号每天一次即可，重复上报无额外收益',
    def: [10],
  },
  {
    key: 'keepalive_enabled',
    kind: 'bool',
    label: '自动保活',
    desc: '定期刷新登录令牌，避免账号因长期闲置掉线',
    def: true,
  },
  {
    key: 'keepalive_hours',
    kind: 'hours',
    label: '保活时刻',
    desc: '在哪些整点刷新令牌（0-23，可多个）',
    def: [22],
  },
  {
    key: 'school_enabled',
    kind: 'bool',
    label: '开学季任务',
    desc: '自动完成开学季任务中心的活动并抽空抽奖余额（仅国内版；国际版由脚本内部跳过）',
    def: true,
  },
  {
    key: 'school_hours',
    kind: 'hours',
    label: '开学季时刻',
    desc: '在哪些整点执行开学季任务（0-23，可多个）。活动未开启时会自动跳过，不算失败',
    def: [12],
  },
  {
    key: 'cat_enabled',
    kind: 'bool',
    label: '夜猫任务',
    desc: '在夜猫窗口（23:00–08:00）补做一次夜猫任务；窗口外会自动跳过',
    def: true,
  },
  {
    key: 'cat_hours',
    kind: 'hours',
    label: '夜猫时刻',
    desc: '在哪些整点尝试夜猫任务（0-23，可多个）。默认凌晨 1 点；窗口内每天最多补一次',
    def: [1],
  },
  {
    key: 'activity_report_count',
    kind: 'num',
    label: '每次上报条数',
    desc: '每个账号每次活跃上报发几条。领养猫需要 5 次对话，默认 5 条一次刷满；填 1 即旧行为',
    unit: '条',
    min: 1,
    max: 20,
    def: 5,
  },
];

const COOLDOWN_FIELDS: Field[] = [
  {
    key: 'soft_rate',
    kind: 'duration',
    label: '软限流冷却基数',
    desc: '被腾讯限流后，账号冷却多久。数值越大越保守（格式如 600s / 10m / 1h）',
    def: '600s',
  },
  {
    key: 'soft_rate_max',
    kind: 'duration',
    label: '冷却退避上限',
    desc: '连续触发限流会逐次延长冷却，这是延长后的封顶值（格式如 2h）',
    def: '2h',
  },
];

const POOL_FIELDS: Field[] = [
  {
    key: 'max_in_flight',
    kind: 'num',
    label: '单账号最大并发',
    desc: '一个账号同时处理几个请求。调大能提高吞吐，但更容易触发腾讯限流（0 = 不限制）',
    unit: '个',
    min: 0,
    max: 32,
    def: 3,
  },
  {
    key: 'breaker_threshold',
    kind: 'num',
    label: '连续失败熔断阈值',
    desc: '某账号连续失败多少次后，自动暂停使用它一段时间',
    unit: '次',
    min: 1,
    max: 100,
    def: 3,
  },
  {
    key: 'breaker_cooldown',
    kind: 'duration',
    label: '熔断基础冷却',
    desc: '被熔断的账号先等待多久（格式如 30m / 1h）',
    def: '30m',
  },
  {
    key: 'breaker_cooldown_max',
    kind: 'duration',
    label: '熔断冷却上限',
    desc: '反复熔断会指数退避延长，这是封顶值（格式如 6h）',
    def: '6h',
  },
  {
    key: 'idle_weight_per_hour',
    kind: 'num',
    label: '闲置补偿 / 小时',
    desc: '账号每闲置 1 小时增加一点调度权重，让久未使用的账号优先被选中',
    min: 0,
    max: 10,
    step: 0.1,
    def: 0.5,
  },
  {
    key: 'idle_weight_max',
    kind: 'num',
    label: '闲置补偿上限',
    desc: '闲置加成的封顶值，避免某个账号权重无限增大',
    min: 0,
    max: 100,
    step: 0.5,
    def: 5,
  },
  {
    key: 'expiring_soon',
    kind: 'duration',
    label: '快过期积分窗口',
    desc: '到期时间落在此窗口内的积分会被标记为「快过期」，选号时优先消耗掉，避免白白过期。留空或填 0 = 关闭该优化',
    def: '168h',
  },
];

const FEATURES_FIELDS: Field[] = [
  {
    key: 'sanitize_blacklist_fingerprints',
    kind: 'bool',
    label: '出站请求指纹脱敏',
    desc: '对发往上游的请求做轻量脱敏，降低被判定异常的概率。除非在排查问题，否则建议保持开启',
    def: true,
  },
];

const SESSION_FIELDS: Field[] = [
  {
    key: 'enabled',
    kind: 'bool',
    label: '会话粘性',
    desc: '同一会话的连续请求尽量路由到同一账号，多轮对话更连贯（多实例部署时依赖 Redis）',
    def: true,
  },
  {
    key: 'ttl',
    kind: 'duration',
    label: '会话保持时长',
    desc: '一次会话多久没活动就解除绑定（格式如 30m / 1h）',
    def: '30m',
  },
  {
    key: 'gc_interval',
    kind: 'duration',
    label: '会话清理周期',
    desc: '后台多久清理一次过期会话（格式如 5m / 10m）',
    def: '5m',
  },
];

const PROMPT_FIELDS: Field[] = [
  {
    key: 'mode',
    kind: 'select',
    label: '系统提示词模式',
    desc: 'passthrough：原样透传客户端传来的 system 消息；custom：网关用自有提示词替换它',
    caution:
      '默认 passthrough 会把下游的 system prompt 原样送给上游。若你依赖网关自己的提示词来稳定行为（或避免 system 指纹被判异常），请改为 custom。',
    options: [
      {value: 'passthrough', label: 'passthrough（透传客户端 system，默认）'},
      {value: 'custom', label: 'custom（替换为网关提示词）'},
    ],
    def: 'passthrough',
  },
  {
    key: 'file',
    kind: 'text',
    label: '自定义提示词文件',
    desc: '仅在 custom 模式下生效。留空使用内置默认提示词',
    caution: '路径必须存在于上游容器内且可读；写错会导致上游启动失败、反代不可用。不确定就留空。',
    placeholder: '留空 = 使用内置默认',
    def: '',
  },
];

const SERVER_FIELDS: Field[] = [
  {
    key: 'max_body_mb',
    kind: 'num',
    label: '请求体上限',
    desc: '单个请求体最大体积，超过返回 413。反代网关也按此值限制入站请求，调大可容纳更长的上下文',
    unit: 'MB',
    min: 1,
    max: 256,
    def: 8,
  },
];

const UPSTREAM_FIELDS: Field[] = [
  {
    key: 'user_agent',
    kind: 'text',
    label: '出站 User-Agent',
    desc: '网关向腾讯发起请求时使用的客户端标识，留空用内置默认（已对齐官方 WorkBuddy）',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'client_name',
    kind: 'text',
    label: '客户端名称',
    desc: '用量归属头（X-Product / X-IDE-Name / X-IDE-Type / X-IDE-Version）的取值，影响官网「使用端」显示。留空 = 对齐官方桌面端（WorkBuddy）；填 SaaS 可还原旧行为',
    placeholder: '留空 = WorkBuddy（对齐官方桌面端）',
    def: '',
  },
  {
    key: 'client_version',
    kind: 'text',
    label: '客户端版本',
    desc: '出站 UA 里 WorkBuddy/<版本> 这段，也用于 X-IDE-Version 头。留空 = 内置默认（对齐官方分发包）',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'cli_version',
    kind: 'text',
    label: 'CLI 版本',
    desc: '出站 UA 里 CLI/<版本> 这段。留空 = 内置默认',
    placeholder: '留空 = 内置默认',
    def: '',
  },
  {
    key: 'device_token_file',
    kind: 'text',
    label: '设备 Token 文件',
    desc: '宿主上存放 device token 的文件路径，留空则不读文件。上游每 5 分钟读一次，读失败自动忽略',
    placeholder: '留空 = 不读文件',
    def: '',
  },
  {
    key: 'passthrough_ip',
    kind: 'bool',
    label: '透传客户端 IP',
    desc: '是否把客户端 IP（X-Forwarded-For / X-Real-IP）透传给上游。缺省关闭——不把内网/代理 IP 暴露给上游',
    def: false,
  },
];

/**
 * 国际版（global）配置。
 *
 * 上游从 2026-09-14 起单实例同时支持国内版与国际版：账号按自身 realm 路由，
 * global.enabled=false 是「锁死纯 CN」的逃生门（关掉后国际版账号会被当 CN 打向
 * 国内端点，上游文档称之为配置错误）。两个 base 留空即用 https://www.workbuddy.ai。
 */
const GLOBAL_FIELDS: Field[] = [
  {
    key: 'enabled',
    kind: 'bool',
    label: '启用国际版路由',
    desc: '开启后，realm=global 的账号走 workbuddy.ai（国际版模型与端点）。关闭 = 锁死纯 CN：此时国际版账号会被当成国内版账号发往国内端点，属配置错误',
    def: true,
  },
  {
    key: 'chat_base',
    kind: 'text',
    label: '国际版 Chat 基址',
    desc: '国际版的聊天 / 登录 / 模型接口基址，留空用内置默认 https://www.workbuddy.ai',
    placeholder: '留空 = https://www.workbuddy.ai',
    def: '',
  },
  {
    key: 'billing_base',
    kind: 'text',
    label: '国际版 Billing 基址',
    desc: '国际版的积分 / trial 接口基址，留空用内置默认 https://www.workbuddy.ai',
    placeholder: '留空 = https://www.workbuddy.ai',
    def: '',
  },
];

type Group = 'schedule' | 'prompt' | 'cooldown' | 'features' | 'session' | 'pool' | 'server' | 'upstream' | 'global';

/** 高级 JSON 编辑器里可直写的上游配置段 */
type WireSection =
  | 'schedule'
  | 'cooldown'
  | 'pool'
  | 'features'
  | 'session_sticky'
  | 'prompt'
  | 'server'
  | 'upstream'
  | 'global';

interface GroupDef {
  id: Group;
  /** 对应的上游 config.json 段名（UI 名与段名不一致时由此映射） */
  section: WireSection;
  title: string;
  desc: string;
  fields: Field[];
}

const GROUPS: GroupDef[] = [
  {
    id: 'schedule',
    section: 'schedule',
    title: '定时任务',
    desc: '六类任务各自独立排程：签到 / 猫猫旅行 / 活跃上报 / 保活 / 开学季 / 夜猫。可分别开关并设置执行时刻。签到、旅行、活跃上报、开学季、夜猫**只对国内版账号生效**——国际版没有这些体系，只有保活照常执行',
    fields: SCHEDULE_FIELDS,
  },
  {
    id: 'prompt',
    section: 'prompt',
    title: '系统提示词',
    desc: '网关如何对待客户端传来的 system / developer 消息',
    fields: PROMPT_FIELDS,
  },
  {
    id: 'cooldown',
    section: 'cooldown',
    title: '限流与冷却',
    desc: '被腾讯限流后的冷却策略',
    fields: COOLDOWN_FIELDS,
  },
  {
    id: 'features',
    section: 'features',
    title: '功能开关',
    desc: '上游的进阶行为开关',
    fields: FEATURES_FIELDS,
  },
  {
    id: 'session',
    section: 'session_sticky',
    title: '会话粘性',
    desc: '多轮对话的路由粘性与清理策略',
    fields: SESSION_FIELDS,
  },
  {
    id: 'pool',
    section: 'pool',
    title: '并发与熔断',
    desc: '控制账号池的并发能力与故障保护',
    fields: POOL_FIELDS,
  },
  {
    id: 'server',
    section: 'server',
    title: '请求上限',
    desc: '网关自身的请求约束',
    fields: SERVER_FIELDS,
  },
  {
    id: 'upstream',
    section: 'upstream',
    title: '出站标识',
    desc: '网关向腾讯发起请求时的客户端标识',
    fields: UPSTREAM_FIELDS,
  },
  {
    id: 'global',
    section: 'global',
    title: '国际版',
    desc: '国际版（workbuddy.ai）路由开关与基址。关闭即锁死纯 CN',
    fields: GLOBAL_FIELDS,
  },
];

type FieldValue = boolean | number | string;

function defaultValues(fields: Field[]): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const f of fields) out[f.key] = f.kind === 'hours' ? hoursToText(f.def) : f.def;
  return out;
}

/** 从配置中取出某个分组的已知字段（缺失或类型不符时回退到默认值） */
function pickValues(fields: Field[], source: Record<string, unknown> | undefined): Record<string, FieldValue> {
  const out: Record<string, FieldValue> = {};
  for (const f of fields) {
    const raw = source?.[f.key];
    switch (f.kind) {
      case 'bool':
        out[f.key] = typeof raw === 'boolean' ? raw : f.def;
        break;
      case 'num': {
        const n = typeof raw === 'number' ? raw : Number(raw);
        out[f.key] = Number.isFinite(n) ? n : f.def;
        break;
      }
      case 'hours':
        // 上游为 []int；表单里用 "9, 21" 这样的字符串承载，保存时再解析回数组
        out[f.key] = Array.isArray(raw) ? hoursToText(raw) : hoursToText(f.def);
        break;
      case 'duration':
        out[f.key] = typeof raw === 'string' && DURATION_RE.test(raw) ? raw : f.def;
        break;
      case 'select':
        // 只接受枚举内的取值；配置里是别的值（上游改过枚举）时回退默认
        out[f.key] = typeof raw === 'string' && f.options.some((o) => o.value === raw) ? raw : f.def;
        break;
      case 'text':
        out[f.key] = typeof raw === 'string' ? raw : f.def;
        break;
    }
  }
  return out;
}

/** 文本类字段的即时校验（用于输入框下方提示，不阻塞输入） */
function fieldError(f: Field, raw: FieldValue): string | undefined {
  if (f.kind === 'num') {
    // 输入框的 min/max 只是浏览器属性，不参与提交校验，这里显式检查
    const n = Number(raw);
    if (!Number.isFinite(n)) return '请填写数字';
    if (n < f.min || n > f.max) {
      return `请填 ${f.min}–${f.max}${f.unit ? `（${f.unit}）` : ''}`;
    }
    return undefined;
  }
  if (f.kind === 'hours') {
    const r = parseHours(String(raw));
    return r.ok ? undefined : r.error;
  }
  if (f.kind === 'duration') {
    return DURATION_RE.test(String(raw).trim()) ? undefined : '格式如 30s / 10m / 2h / 1d';
  }
  if (f.kind === 'text' && /[\r\n\u0000-\u001f]/.test(String(raw))) {
    return '不能包含换行或控制字符';
  }
  return undefined;
}

/** 把表单值转换成要写入上游 config.json 的值 */
function toWire(
  f: Field,
  raw: FieldValue,
): {ok: true; value: boolean | number | number[] | string} | {ok: false; error: string} {
  if (f.kind === 'hours') {
    const r = parseHours(String(raw));
    return r.ok ? {ok: true, value: r.hours} : {ok: false, error: r.error ?? '时刻格式有误'};
  }
  if (f.kind === 'duration') {
    const t = String(raw).trim();
    return DURATION_RE.test(t) ? {ok: true, value: t} : {ok: false, error: '格式如 30s / 10m / 2h / 1d'};
  }
  if (f.kind === 'num') {
    const n = Number(raw);
    if (!Number.isFinite(n)) return {ok: false, error: '请填写数字'};
    if (n < f.min || n > f.max) {
      return {ok: false, error: `请填 ${f.min}–${f.max}${f.unit ? `（${f.unit}）` : ''}`};
    }
    return {ok: true, value: n};
  }
  if (f.kind === 'select') {
    const v = String(raw);
    return f.options.some((o) => o.value === v)
      ? {ok: true, value: v}
      : {ok: false, error: '取值不在允许范围内'};
  }
  if (f.kind === 'text') {
    const t = String(raw).trim();
    if (/[\r\n\u0000-\u001f]/.test(t)) return {ok: false, error: '不能包含换行或控制字符'};
    return {ok: true, value: t};
  }
  return {ok: true, value: raw};
}

const FIELD_BY_KEY: Record<string, Field> = {};
for (const g of GROUPS) for (const f of g.fields) FIELD_BY_KEY[f.key] = f;

export default function SettingsPage() {
  const {isAdmin} = useAuth();
  const [cfg, setCfg] = useState<UpstreamConfig | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  /** 模型列表来源：dynamic = 上游实时拉取，static = 上游内置回退表 */
  const [modelSource, setModelSource] = useState<ModelSource>('unknown');
  const [modelsLoading, setModelsLoading] = useState(false);

  /** 可视化表单状态 */
  const [form, setForm] = useState<Record<Group, Record<string, FieldValue>>>({
    schedule: defaultValues(SCHEDULE_FIELDS),
    prompt: defaultValues(PROMPT_FIELDS),
    cooldown: defaultValues(COOLDOWN_FIELDS),
    pool: defaultValues(POOL_FIELDS),
    features: defaultValues(FEATURES_FIELDS),
    session: defaultValues(SESSION_FIELDS),
    server: defaultValues(SERVER_FIELDS),
    upstream: defaultValues(UPSTREAM_FIELDS),
    global: defaultValues(GLOBAL_FIELDS),
  });
  /** 加载时的原始值，用于只提交改动过的项 */
  const original = useRef<Record<Group, Record<string, FieldValue>>>({
    schedule: defaultValues(SCHEDULE_FIELDS),
    prompt: defaultValues(PROMPT_FIELDS),
    cooldown: defaultValues(COOLDOWN_FIELDS),
    pool: defaultValues(POOL_FIELDS),
    features: defaultValues(FEATURES_FIELDS),
    session: defaultValues(SESSION_FIELDS),
    server: defaultValues(SERVER_FIELDS),
    upstream: defaultValues(UPSTREAM_FIELDS),
    global: defaultValues(GLOBAL_FIELDS),
  });
  /** 高级模式（直接编辑 JSON） */
  const [advanced, setAdvanced] = useState(false);
  const [schedText, setSchedText] = useState('');
  const [coolText, setCoolText] = useState('');
  const [poolText, setPoolText] = useState('');
  const [featText, setFeatText] = useState('');
  const [sessText, setSessText] = useState('');
  const [promptText, setPromptText] = useState('');
  const [serverText, setServerText] = useState('');
  const [upText, setUpText] = useState('');
  const [globalText, setGlobalText] = useState('');

  const [modelMap, setModelMap] = useState<Record<string, string>>({});
  const [mapAlias, setMapAlias] = useState('');
  const [mapTarget, setMapTarget] = useState('');
  const [users, setUsers] = useState<UserItem[]>([]);
  const [newUser, setNewUser] = useState({username: '', password: '', role: 'viewer'});
  const [busy, setBusy] = useState(false);
  /** Upstash（Redis 持久化）表单 */
  const [upstashForm, setUpstashForm] = useState({url: '', token: ''});
  const [upstashBusy, setUpstashBusy] = useState(false);

  const load = useCallback(async () => {
    // 本地数据很快（配置/映射/用户），先取到即渲染，不被上游探测拖慢
    const [c, mm, u] = await Promise.allSettled([
      settingsApi.upstream(),
      settingsApi.modelMap(),
      settingsApi.users(),
    ]);
    if (c.status === 'fulfilled') {
      const v = c.value;
      setCfg(v);
      if (v.available !== false) {
        const picked = {
          schedule: pickValues(SCHEDULE_FIELDS, v.schedule),
          prompt: pickValues(PROMPT_FIELDS, v.prompt),
          cooldown: pickValues(COOLDOWN_FIELDS, v.cooldown),
          pool: pickValues(POOL_FIELDS, v.pool),
          features: pickValues(FEATURES_FIELDS, v.features),
          session: pickValues(SESSION_FIELDS, v.session_sticky),
          server: pickValues(SERVER_FIELDS, v.server),
          upstream: pickValues(UPSTREAM_FIELDS, v.upstream),
          global: pickValues(GLOBAL_FIELDS, v.global),
        };
        setForm(picked);
        original.current = {
          schedule: {...picked.schedule},
          prompt: {...picked.prompt},
          cooldown: {...picked.cooldown},
          pool: {...picked.pool},
          features: {...picked.features},
          session: {...picked.session},
          server: {...picked.server},
          upstream: {...picked.upstream},
          global: {...picked.global},
        };
        setSchedText(JSON.stringify(v.schedule ?? {}, null, 2));
        setCoolText(JSON.stringify(v.cooldown ?? {}, null, 2));
        setPoolText(JSON.stringify(v.pool ?? {}, null, 2));
        setFeatText(JSON.stringify(v.features ?? {}, null, 2));
        setSessText(JSON.stringify(v.session_sticky ?? {}, null, 2));
        setPromptText(JSON.stringify(v.prompt ?? {}, null, 2));
        setServerText(JSON.stringify(v.server ?? {}, null, 2));
        setUpText(JSON.stringify(v.upstream ?? {}, null, 2));
        setGlobalText(JSON.stringify(v.global ?? {}, null, 2));
        // url 可回显；token 不回显明文，留空表示不修改
        setUpstashForm({url: v.upstash?.url || '', token: ''});
      }
    }
    if (mm.status === 'fulfilled') setModelMap(mm.value);
    if (u.status === 'fulfilled') setUsers(u.value);
  }, []);

  /**
   * 上游模型列表。
   *
   * 上游自己会缓存 1 小时（动态拉取成功时），失败则回退到编译进二进制的
   * 静态表、并有 5 分钟负缓存——所以「刷新页面」不一定能拿到新列表。
   * 这也是为什么提供手动重新拉取：上游刷新令牌/新增模型后，用户需要能
   * 立刻主动取一次，而不是干等缓存过期。
   */
  const loadModels = useCallback(async (silent = true) => {
    if (!silent) setModelsLoading(true);
    try {
      const res = await upstreamApi.models();
      setModels(res.models || []);
      setModelSource(res.source || 'unknown');
    } catch {
      setModels([]);
      setModelSource('unknown');
    } finally {
      if (!silent) setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    loadModels();
  }, [load, loadModels]);

  const upstreamReady = !!cfg && cfg.available !== false;
  const upstreamError = cfg && cfg.available === false ? cfg.error : undefined;
  /** 配置文件中是否已保存 Upstash 地址 */
  const upstashConfigured = !!cfg?.upstash?.url;

  /** 保存 Upstash 配置（token 留空表示保持原值） */
  async function saveUpstash() {
    if (!upstashForm.url.trim()) {
      notify.err('请填写 Upstash 地址');
      return;
    }
    setUpstashBusy(true);
    try {
      await settingsApi.saveUpstream({
        upstash: {
          url: upstashForm.url.trim(),
          ...(upstashForm.token.trim() ? {token: upstashForm.token.trim()} : {}),
        },
      });
      notify.ok('Upstash 配置已保存', '正在自动应用到上游…');
      setUpstashForm((f) => ({...f, token: ''}));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setUpstashBusy(false);
    }
  }

  function setField(group: Group, key: string, value: FieldValue) {
    setForm((prev) => ({...prev, [group]: {...prev[group], [key]: value}}));
  }

  /** 是否有未保存的改动 */
  function isDirty(group: Group): boolean {
    const cur = form[group];
    const org = original.current[group];
    return Object.keys(cur).some((k) => cur[k] !== org[k]);
  }

  /** 只提交改动过的字段，避免覆盖其他未展示的配置项 */
  async function saveGroup(group: Group) {
    const cur = form[group];
    const org = original.current[group];
    const patch: Record<string, boolean | number | number[] | string> = {};
    for (const k of Object.keys(cur)) {
      if (cur[k] === org[k]) continue;
      const f = FIELD_BY_KEY[k];
      if (!f) continue;
      const w = toWire(f, cur[k]);
      if (!w.ok) {
        notify.err(`「${f.label}」填写有误`, w.error);
        return;
      }
      patch[k] = w.value;
    }
    if (!Object.keys(patch).length) {
      notify.info('没有需要保存的改动');
      return;
    }
    setBusy(true);
    try {
      const def = GROUPS.find((g) => g.id === group);
      await settingsApi.saveUpstream({[def?.section ?? group]: patch});
      notify.ok('设置已保存', '正在自动应用到上游…');
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function resetGroup(group: Group) {
    setForm((prev) => ({...prev, [group]: {...original.current[group]}}));
  }

  /** 高级模式：直接保存 JSON（字段名即上游 config.json 的段名） */
  async function saveJson(field: WireSection, text: string) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      notify.err('JSON 格式有误，请检查括号与逗号');
      return;
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      notify.err('需要是一个 JSON 对象，例如 { "checkin_hours": [9, 21] }');
      return;
    }
    setBusy(true);
    try {
      await settingsApi.saveUpstream({[field]: parsed});
      notify.ok('设置已保存', '正在自动应用到上游…');
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveModelMap(next: Record<string, string>) {
    try {
      await settingsApi.saveModelMap(next);
      setModelMap(next);
      notify.ok('模型映射已保存');
    } catch (e) {
      notify.err(errText(e));
    }
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title="设置"
        description="上游代理配置、模型别名映射与管理端用户"
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            title="重新读取上游配置与模型列表（上游模型列表自身有 1 小时缓存）"
            onClick={() => {
              load();
              loadModels(false);
            }}
          >
            <RefreshCw />
            刷新配置与模型
          </Button>
        }
      />

      <Tabs defaultValue="upstream">
        {/* 标签较多，手机上会撑破容器，这里允许横向滚动 */}
        <div className="-mx-1 overflow-x-auto px-1 pb-1">
        <TabsList className="w-max">
          <TabsTrigger value="upstream"><Server className="mr-1.5 h-3.5 w-3.5" />上游配置</TabsTrigger>
          <TabsTrigger value="models"><Shuffle className="mr-1.5 h-3.5 w-3.5" />模型映射</TabsTrigger>
          <TabsTrigger value="users"><Users className="mr-1.5 h-3.5 w-3.5" />管理用户</TabsTrigger>
          <TabsTrigger value="system"><DownloadCloud className="mr-1.5 h-3.5 w-3.5" />系统更新</TabsTrigger>
          <TabsTrigger value="changelog"><FileText className="mr-1.5 h-3.5 w-3.5" />更新日志</TabsTrigger>
          <TabsTrigger value="about"><Info className="mr-1.5 h-3.5 w-3.5" />关于</TabsTrigger>
        </TabsList>
        </div>

        {/* ═══ 上游配置 ═══ */}
        <TabsContent value="upstream" className="mt-3 space-y-3">
          {upstreamError && (
            <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div className="space-y-1">
                <div className="text-xs font-medium">无法读取上游配置</div>
                <div className="text-[11px] text-muted-foreground">{upstreamError}</div>
                <div className="text-[11px] text-muted-foreground">
                  请确认 workbuddy2api 已部署且路径正确（环境变量 <code className="font-mono">WB_UPSTREAM_CONFIG</code>）。
                  在读取成功前，下方配置项已锁定，避免误写空配置覆盖真实文件。
                </div>
              </div>
            </div>
          )}

          {/* 账号池概况 */}
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <div className="rounded-[20px] bg-muted px-3.5 py-3">
              <div className="mb-2 text-sm font-medium">服务信息</div>
              <div className="space-y-1.5 text-xs">
                {([
                  ['上游地址', cfg?.listen ? `127.0.0.1${cfg.listen}` : '—'],
                  ['接入密钥', cfg?.api_key_masked ? '已配置（已隐藏）' : '—'],
                  ['账号目录', cfg?.auth_dir || '—'],
                ] as [string, string][]).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between gap-3">
                    <span className="shrink-0 text-muted-foreground">{k}</span>
                    <span className="min-w-0 flex-1 truncate text-right font-mono" title={v}>{v}</span>
                    {k === '账号目录' && v !== '—' && (
                      <CopyButton value={v} title="复制账号目录" className="h-6 w-6" />
                    )}
                  </div>
                ))}
                {cfg?.upstream_auth_dir && (
                  <div className="flex items-start justify-between gap-3">
                    <span className="shrink-0 text-muted-foreground">上游声明目录</span>
                    <span
                      className="min-w-0 flex-1 truncate text-right font-mono text-amber-600 dark:text-amber-400"
                      title={cfg.upstream_auth_dir}
                    >
                      {cfg.upstream_auth_dir}
                    </span>
                  </div>
                )}
              </div>
              {cfg?.upstream_auth_dir && (
                <p className="mt-2 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
                  上游配置里声明的账号目录与本站读取的不一致，管理端实际以「账号目录」为准。
                </p>
              )}
            </div>

            <div className="rounded-[20px] bg-muted px-3.5 py-3 lg:col-span-2">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <div className="text-sm font-medium">可用模型</div>
                  {modelSource === 'dynamic' && (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      上游实时列表 · {models.length} 个
                    </span>
                  )}
                  {modelSource === 'static' && (
                    <span
                      className="shrink-0 text-[11px] text-amber-600 dark:text-amber-400"
                      title="上游动态拉取失败时，会回退到其内置的静态模型表——那是编译进二进制的固定列表，数量比实际可用模型少。可直接点击右侧「重新拉取」再试一次。"
                    >
                      上游内置回退表（非实时） · {models.length} 个
                    </span>
                  )}
                  {modelSource === 'unknown' && models.length > 0 && (
                    <span className="shrink-0 text-[11px] text-muted-foreground">
                      {models.length} 个
                    </span>
                  )}
                </div>
                {/*
                  只在请求进行中禁用，不绑定「配置文件是否可读」——这两件事无关：
                  模型列表是从上游 /v1/models 实时取的，config.json 读不到不代表
                  上游不可达。把按钮一起禁用会让用户在最需要重试时点不动。
                */}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 shrink-0 gap-1.5 text-[11px]"
                  disabled={modelsLoading}
                  title="重新向上游拉取模型列表（上游自身有 1 小时缓存，失败时回退静态表）"
                  onClick={() => loadModels(false)}
                >
                  {modelsLoading ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="h-3.5 w-3.5" />
                  )}
                  重新拉取
                </Button>
              </div>
              <div className="flex flex-wrap gap-1">
                {models.length ? (
                  models.map((m) => (
                    <Badge key={m.id} variant="secondary" className="rounded-full font-mono text-[10px]">
                      {m.id}
                    </Badge>
                  ))
                ) : (
                  <span className="text-xs text-muted-foreground">
                    {upstreamReady ? '暂时没取到模型列表，请确认上游容器在运行' : '配置未就绪，暂无法获取模型'}
                  </span>
                )}
              </div>

              {/* 关键说明：模型列表由上游**随机挑一个健康账号**去拉，取决于该账号的
                  授权，所以「同样的部署、不同账号看到的模型数量不同」是正常的——
                  并非本站少显示。上游自身还会缓存 1 小时。 */}
              {models.length > 0 && (
                <p className="mt-2 text-[10px] leading-4 text-muted-foreground/80">
                  {modelSource === 'static' ? (
                    <>
                      上游动态拉取失败，已回退到它<b>编译进二进制的静态表</b>——数量比实际可用模型少。
                      可点击「重新拉取」再试，或重启上游容器后重试。
                    </>
                  ) : (
                    <>
                      列表由上游随机选取的一个健康账号拉取，<b>取决于该账号的授权</b>——
                      不同账号（企业 / 套餐）可见的模型数量可能不同；上游自身缓存 1 小时。
                    </>
                  )}
                </p>
              )}
            </div>
          </div>

          {/* 可视化设置卡片 */}
          {GROUPS.map((g) => {
            const dirty = isDirty(g.id);
            return (
              <div key={g.id} className="rounded-[20px] bg-muted px-3.5 py-3">
                <div className="mb-2.5 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium">{g.title}</div>
                    <div className="text-[11px] text-muted-foreground">{g.desc}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    {dirty && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="rounded-full text-muted-foreground"
                        disabled={busy}
                        onClick={() => resetGroup(g.id)}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        撤销
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant={dirty ? 'default' : 'outline'}
                      className="rounded-full"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveGroup(g.id)}
                    >
                      <Save className="h-3.5 w-3.5" />
                      保存
                    </Button>
                  </div>
                </div>

                {/* 宽屏两列：开关与它对应的时刻/数值字段天然成对，行数减半 */}
                <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
                  {g.fields.map((f) => {
                    const err = fieldError(f, form[g.id][f.key]);
                    const caution = 'caution' in f ? f.caution : undefined;
                    return (
                      <div
                        key={f.key}
                        className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2"
                      >
                        <div className="min-w-0">
                          <div className="text-xs font-medium">{f.label}</div>
                          <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{f.desc}</div>
                          {f.kind !== 'bool' && err && (
                            <div className="mt-0.5 text-[10px] leading-3 text-destructive">{err}</div>
                          )}
                        </div>

                        {f.kind === 'bool' ? (
                          <Switch
                            checked={!!form[g.id][f.key]}
                            disabled={!isAdmin || !upstreamReady}
                            onCheckedChange={(v) => setField(g.id, f.key, v)}
                          />
                        ) : f.kind === 'num' ? (
                          <div className="flex shrink-0 items-center gap-1.5">
                            <Input
                              type="number"
                              min={f.min}
                              max={f.max}
                              step={f.step ?? 1}
                              value={String(form[g.id][f.key] ?? f.def)}
                              disabled={!isAdmin || !upstreamReady}
                              onChange={(e) => {
                                const n = Number(e.target.value);
                                setField(g.id, f.key, Number.isFinite(n) ? n : f.def);
                              }}
                              className={
                                'h-8 w-20 bg-background text-right tabular-nums' +
                                (err ? ' border-destructive' : '')
                              }
                            />
                            {f.unit && (
                              <span className="w-8 text-[11px] text-muted-foreground">{f.unit}</span>
                            )}
                          </div>
                        ) : f.kind === 'select' ? (
                          <Select
                            value={String(form[g.id][f.key] ?? f.def)}
                            disabled={!isAdmin || !upstreamReady}
                            onValueChange={(v) => setField(g.id, f.key, v)}
                          >
                            <SelectTrigger className="h-8 w-[168px] shrink-0 bg-background text-xs">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {f.options.map((o) => (
                                <SelectItem key={o.value} value={o.value} className="text-xs">
                                  {o.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        ) : f.kind === 'text' ? (
                          <Input
                            value={String(form[g.id][f.key] ?? '')}
                            disabled={!isAdmin || !upstreamReady}
                            placeholder={f.placeholder}
                            onChange={(e) => setField(g.id, f.key, e.target.value)}
                            className={
                              'h-8 shrink-0 bg-background text-xs ' +
                              (caution ? 'w-56' : 'w-40') +
                              (err ? ' border-destructive' : '')
                            }
                          />
                        ) : (
                          <Input
                            value={String(form[g.id][f.key] ?? '')}
                            disabled={!isAdmin || !upstreamReady}
                            placeholder={f.kind === 'hours' ? '9, 21' : '600s'}
                            onChange={(e) => setField(g.id, f.key, e.target.value)}
                            className={
                              'h-8 shrink-0 bg-background text-right tabular-nums ' +
                              (f.kind === 'hours' ? 'w-32' : 'w-24') +
                              (err ? ' border-destructive' : '')
                            }
                          />
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* 危险项警示：填错会导致上游启动失败，单独占一行说明 */}
                {g.fields.some((f) => 'caution' in f && f.caution) && (
                  <div className="mt-1.5 space-y-1">
                    {g.fields.map((f) =>
                      'caution' in f && f.caution ? (
                        <div
                          key={f.key}
                          className="flex items-start gap-1.5 rounded-xl bg-amber-500/10 px-3 py-2 text-[11px] leading-4 text-amber-600 dark:text-amber-400"
                        >
                          <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
                          <span>{f.caution}</span>
                        </div>
                      ) : null,
                    )}
                  </div>
                )}

                {!isAdmin && (
                  <p className="mt-2 text-[11px] text-muted-foreground">只读角色无法修改设置。</p>
                )}
              </div>
            );
          })}

          {/* Redis / Upstash 持久化 */}
          <div className="rounded-[20px] bg-muted p-4">
            <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="flex items-center gap-2 text-sm font-medium">
                  Redis 持久化（Upstash）
                  {upstashConfigured ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                      已配置
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="rounded-full text-muted-foreground">
                      未配置
                    </Badge>
                  )}
                </div>
                <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                  {upstashConfigured
                    ? '会话粘性与账号状态由 Upstash 共享存储，多实例部署时状态一致'
                    : '当前为纯内存模式（noop）：状态仅存于容器内。单实例下属正常状态；扩展到多实例或希望重启后保留状态时再配置'}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  disabled={!isAdmin || upstashBusy}
                  onClick={async () => {
                    setUpstashBusy(true);
                    try {
                      const r = await settingsApi.testUpstash(upstashForm.url, upstashForm.token || undefined);
                      (r.ok ? notify.ok : notify.err)(r.message, r.ok ? 'Upstash 可用' : undefined);
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setUpstashBusy(false);
                    }
                  }}
                >
                  {upstashBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
                  测试连接
                </Button>
                <Button
                  size="sm"
                  className="rounded-full"
                  disabled={!isAdmin || upstashBusy || !upstreamReady}
                  onClick={saveUpstash}
                >
                  <Save className="h-3.5 w-3.5" />
                  保存
                </Button>
              </div>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">Upstash 地址</Label>
                <Input
                  value={upstashForm.url}
                  disabled={!isAdmin || !upstreamReady}
                  onChange={(e) => setUpstashForm({...upstashForm, url: e.target.value})}
                  placeholder="https://xxx-12345.upstash.io"
                  className="bg-background font-mono text-xs"
                />
                <div className="text-[11px] text-muted-foreground">
                  在 Upstash 控制台 <span className="font-mono">REST API</span> 一栏复制端点地址
                </div>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">Upstash Token</Label>
                <Input
                  type="password"
                  value={upstashForm.token}
                  disabled={!isAdmin || !upstreamReady}
                  onChange={(e) => setUpstashForm({...upstashForm, token: e.target.value})}
                  placeholder={cfg?.upstash?.has_token ? `已保存：${cfg.upstash.token_masked}（留空则不修改）` : '粘贴 REST API Token'}
                  className="bg-background font-mono text-xs"
                />
                <div className="text-[11px] text-muted-foreground">
                  {cfg?.upstash?.has_token
                    ? '出于安全考虑不回显明文；留空即保持原值不变'
                    : '与上面的地址配套的 Token'}
                </div>
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              {upstashConfigured && (
                <ConfirmDialog
                  title="关闭 Redis 持久化？"
                  description="将清空 Upstash 地址与 Token，上游会退回纯内存模式（noop）。保存后会自动重载上游。"
                  confirmText="清空配置"
                  destructive
                  onConfirm={async () => {
                    setUpstashBusy(true);
                    try {
                      await settingsApi.saveUpstream({upstash: {clear: true}});
                      setUpstashForm({url: '', token: ''});
                      notify.ok('已关闭 Redis 持久化', '正在自动应用到上游');
                      await load();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setUpstashBusy(false);
                    }
                  }}
                  trigger={
                    <Button size="sm" variant="ghost" className="rounded-full text-red-500" disabled={!isAdmin || upstashBusy}>
                      <Trash2 className="h-3.5 w-3.5" />
                      清空配置
                    </Button>
                  }
                />
              )}
            </div>
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
              保存后会自动重启上游使其生效（约 0.5 秒，在途请求会正常完成）。
              Upstash 在容器启动时连接，因此无需手动重启；
              配置有误时上游会自动降级为 noop 并打印警告，不会导致服务不可用。
            </div>
          </div>

          {/* 高级模式：直接编辑 JSON */}
          <div className="rounded-[20px] bg-muted p-4">
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              className="flex w-full items-center justify-between gap-2 text-left"
            >
              <div>
                <div className="text-sm font-medium">高级设置</div>
                <div className="text-[11px] text-muted-foreground">
                  需要配置上面没有提到的参数时，可直接编辑原始 JSON
                </div>
              </div>
              <ChevronDown
                className={'h-4 w-4 shrink-0 text-muted-foreground transition-transform ' + (advanced ? 'rotate-180' : '')}
              />
            </button>

            {advanced && (
              <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">schedule</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('schedule', schedText)}
                    >
                      保存
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? schedText : ''}
                    placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                    onChange={(e) => setSchedText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">cooldown</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('cooldown', coolText)}
                    >
                      保存
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? coolText : ''}
                    placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                    onChange={(e) => setCoolText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">features</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('features', featText)}
                    >
                      保存
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? featText : ''}
                    placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                    onChange={(e) => setFeatText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">session_sticky</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('session_sticky', sessText)}
                    >
                      保存
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? sessText : ''}
                    placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                    onChange={(e) => setSessText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <div className="font-mono text-[11px] text-muted-foreground">pool</div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 rounded-full text-[11px]"
                      disabled={!isAdmin || busy || !upstreamReady}
                      onClick={() => saveJson('pool', poolText)}
                    >
                      保存
                    </Button>
                  </div>
                  <Textarea
                    rows={8}
                    spellCheck={false}
                    disabled={!isAdmin || !upstreamReady}
                    value={upstreamReady ? poolText : ''}
                    placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                    onChange={(e) => setPoolText(e.target.value)}
                    className="bg-background font-mono text-xs"
                  />
                </div>
                {([
                  ['prompt', promptText, setPromptText],
                  ['server', serverText, setServerText],
                  ['upstream', upText, setUpText],
                  ['global', globalText, setGlobalText],
                ] as const).map(([name, val, setter]) => (
                  <div key={name} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="font-mono text-[11px] text-muted-foreground">{name}</div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 rounded-full text-[11px]"
                        disabled={!isAdmin || busy || !upstreamReady}
                        onClick={() => saveJson(name, val)}
                      >
                        保存
                      </Button>
                    </div>
                    <Textarea
                      rows={8}
                      spellCheck={false}
                      disabled={!isAdmin || !upstreamReady}
                      value={upstreamReady ? val : ''}
                      placeholder={upstreamReady ? undefined : '未读取到上游配置，无法编辑'}
                      onChange={(e) => setter(e.target.value)}
                      className="bg-background font-mono text-xs"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </TabsContent>

        {/* ═══ 模型映射 ═══ */}
        <TabsContent value="models" className="mt-4 space-y-4">
          <div className="rounded-[20px] bg-muted p-4">
            <div className="mb-1 text-sm font-medium">新增模型别名</div>
            <div className="mb-3 text-[11px] text-muted-foreground">
              让下游用一个自己熟悉的名字调用某个模型。例如把 <code className="font-mono">gpt-4o-mini</code> 指向{' '}
              <code className="font-mono">glm-5.2</code>。
            </div>
            <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">下游使用的名字</Label>
                <Input value={mapAlias} onChange={(e) => setMapAlias(e.target.value)} placeholder="gpt-4o-mini" className="bg-background" disabled={!isAdmin} />
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">实际调用模型</Label>
                <Select value={mapTarget} onValueChange={setMapTarget} disabled={!isAdmin}>
                  <SelectTrigger className="bg-background"><SelectValue placeholder="选择模型" /></SelectTrigger>
                  <SelectContent>
                    {models.map((m) => (
                      <SelectItem key={m.id} value={m.id}>{m.id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="sm:col-span-2">
                <Button
                  className="rounded-full"
                  disabled={!isAdmin || !mapAlias.trim() || !mapTarget}
                  onClick={() => {
                    saveModelMap({...modelMap, [mapAlias.trim()]: mapTarget});
                    setMapAlias('');
                    setMapTarget('');
                  }}
                >
                  <Plus />
                  添加映射
                </Button>
              </div>
            </div>
          </div>

          <div className="overflow-hidden rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">下游使用的名字</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">实际调用模型</TableHead>
                  {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">操作</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(modelMap).map(([alias, target]) => (
                  <TableRow key={alias} className="border-b border-border/40">
                    <TableCell className="pl-4 font-mono text-xs">{alias}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{target}</TableCell>
                    {isAdmin && (
                      <TableCell className="pr-4 text-right">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                          onClick={() => {
                            const next = {...modelMap};
                            delete next[alias];
                            saveModelMap(next);
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
            {!Object.keys(modelMap).length && (
              <div className="py-10 text-center text-xs text-muted-foreground">
                暂无映射，下游可直接使用上方「可用模型」中的名字
              </div>
            )}
          </div>
        </TabsContent>

        {/* ═══ 管理用户 ═══ */}
        <TabsContent value="users" className="mt-4 space-y-4">
          {isAdmin && (
            <div className="rounded-[20px] bg-muted p-4">
              <div className="mb-1 text-sm font-medium">新增登录用户</div>
              <div className="mb-3 text-[11px] text-muted-foreground">
                管理员可以增删账号与改设置；只读用户只能查看，适合给同事看状态用。
              </div>
              <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">用户名</Label>
                  <Input value={newUser.username} onChange={(e) => setNewUser({...newUser, username: e.target.value})} className="bg-background" />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">密码</Label>
                  <Input type="password" value={newUser.password} onChange={(e) => setNewUser({...newUser, password: e.target.value})} className="bg-background" />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">权限</Label>
                  <Select value={newUser.role} onValueChange={(v) => setNewUser({...newUser, role: v})}>
                    <SelectTrigger className="bg-background"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="admin">管理员（可修改）</SelectItem>
                      <SelectItem value="viewer">只读用户（仅查看）</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  className="rounded-full"
                  disabled={busy}
                  onClick={async () => {
                    if (!newUser.username.trim() || !newUser.password) {
                      notify.err('请填写用户名与密码');
                      return;
                    }
                    setBusy(true);
                    try {
                      await settingsApi.addUser(newUser);
                      notify.ok('用户已创建');
                      setNewUser({username: '', password: '', role: 'viewer'});
                      load();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <Plus />
                  创建
                </Button>
              </div>
            </div>
          )}

          <div className="overflow-hidden rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">用户名</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">权限</TableHead>
                  {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">操作</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((u) => (
                  <TableRow key={u.username} className="border-b border-border/40">
                    <TableCell className="pl-4 text-sm font-medium">{u.username}</TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="rounded-full">
                        {u.role === 'admin' ? '管理员' : '只读'}
                      </Badge>
                    </TableCell>
                    {isAdmin && (
                      <TableCell className="pr-4 text-right">
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 rounded-full text-xs"
                            onClick={async () => {
                              const pwd = window.prompt(
                                `为「${u.username}」设置新密码（至少 8 位）：`);
                              if (!pwd) return;
                              try {
                                const r = await settingsApi.updateUser(u.username, {password: pwd});
                                // 改密码会吊销该用户既有会话。若改的是自己，
                                // 当前登录态也随之失效——必须明确告知要去重新登录，
                                // 否则用户会以为「界面卡住了」（下次请求就是 401）。
                                if (r?.relogin_required) {
                                  notify.ok('密码已更新', '当前登录状态已失效，请用新密码重新登录');
                                  window.setTimeout(() => {
                                    window.location.href = '/login';
                                  }, 1800);
                                  return;
                                }
                                notify.ok('密码已更新', '该用户的其他登录状态已全部失效');
                              } catch (e) {
                                notify.err(errText(e));
                              }
                            }}
                          >
                            重置密码
                          </Button>
                          <ConfirmDialog
                            title={`删除用户「${u.username}」？`}
                            description="删除后该用户将无法登录管理端。"
                            confirmText="删除"
                            destructive
                            onConfirm={async () => {
                              try {
                                await settingsApi.removeUser(u.username);
                                notify.ok('已删除');
                                load();
                              } catch (e) {
                                notify.err(errText(e));
                              }
                            }}
                            trigger={
                              <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600">
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            }
                          />
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {!users.length && (
              <EmptyState
                icon={Users}
                title="暂无管理用户"
                description="至少保留一个管理员账号"
                className="flex flex-col items-center justify-center py-12 text-center"
              />
            )}
          </div>
        </TabsContent>

        {/* ═══ 系统更新 ═══ */}
        <TabsContent value="system" className="mt-4 space-y-4">
          <UpdatePanel />
        </TabsContent>

        {/* ═══ 更新日志 ═══ */}
        <TabsContent value="changelog" className="mt-4">
          <ChangelogPanel />
        </TabsContent>

        {/* ═══ 关于 ═══ */}
        <TabsContent value="about" className="mt-4">
          <div className="rounded-[20px] bg-muted p-5 text-xs leading-6 text-muted-foreground">
            <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
              <SettingsIcon className="h-4 w-4" />
              WorkBuddy Manager
            </div>
            <p>
              本项目为 workbuddy2api（腾讯 CodeBuddy → OpenAI 兼容代理）提供网页管理界面与对外接口网关。
              账号轮询、并发与熔断由 workbuddy2api 负责；本管理端负责账号纳管、密钥分发、IP 管控、请求日志与用量统计。
            </p>
            <p className="mt-3">
              界面风格参考 linux-do/cdk（MIT），特此致谢。
            </p>
            <p className="mt-3">
              下游接入：Base URL 填 <span className="font-mono">https://你的域名/v1</span>，密钥用「API 密钥」页生成的{' '}
              <span className="font-mono">wbk_...</span>。
            </p>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
