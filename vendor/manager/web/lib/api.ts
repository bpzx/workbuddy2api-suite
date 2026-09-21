import axios, {AxiosError} from 'axios';
import {tp} from './i18n';
import type {Realm} from './realm-context';
import type {
  Account,
  CheckinLogPage,
  CreditExpiry,
  CreditsMeta,
  AccountsResponse,
  ApiKey,
  IpAccessLog,
  IpRule,
  Me,
  AuditLogPage,
  ModelCatalog,
  ModelListResponse,
  Page,
  PlaygroundModels,
  ReloadState,
  RequestLog,
  Changelog,
  SecurityConfig,
  StatsSummary,
  TaskLogResponse,
  TaskRunStatus,
  UpstreamConfig,
  UpstreamStatus,
  UsageBreakdown,
  UpdateCheck,
  UpdateStatus,
  UsagePoint,
  UserItem,
  Versions,
} from './types';

export const http = axios.create({
  baseURL: '',
  withCredentials: true,
  timeout: 60000,
});

/**
 * 统一抽取后端错误信息。
 *
 * 后端的报错是中文（服务端不做多语言，见 README 的多语言说明），这里过一遍
 * 短语表：命中已收录的后端文案就换成当前语言，没收录的原样展示——既不需要
 * 后端改造，也不会因为漏收录而显示成键名或空白。
 */
export function errText(e: unknown): string {
  const ax = e as AxiosError<{detail?: string; error?: string}>;
  const d = ax?.response?.data;
  const raw = (typeof d === 'string' ? d : d?.detail || d?.error) || ax?.message || '';
  return raw ? tp(raw) : tp('请求失败');
}

http.interceptors.response.use(
  (r) => r,
  (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      // 会话失效：清掉缓存的登录态，避免仍显示管理员入口
      window.sessionStorage.removeItem('wb-me');
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  },
);

const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
const post = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;
const patch = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.patch<T>(url, body)).data;
const put = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.put<T>(url, body)).data;
const del = async <T>(url: string): Promise<T> => (await http.delete<T>(url)).data;

/* ── 鉴权 ───────────────────────────────────────────── */
export const authApi = {
  me: () => get<Me>('/api/me'),
  login: (username: string, password: string) =>
    post<{ok: boolean; username: string; role: string}>('/api/login', {username, password}),
  logout: () => post<{ok: boolean}>('/api/logout'),
  /** 吊销当前用户的**全部**会话（含本机）——服务端递增会话版本，所有 cookie 立即失效。 */
  revokeSessions: () => post<{ok: boolean; relogin_required: boolean}>('/api/sessions/revoke'),
};

/* ── 账号 ───────────────────────────────────────────── */
export const accountApi = {
  list: () => get<AccountsResponse>('/api/accounts'),
  /** 发起扫码登录；realm 决定国内版 / 国际版端点 */
  start: (realm: Realm = 'cn') =>
    post<{state: string; authUrl: string; realm: Realm}>('/api/auth/start', {realm}),
  /** 轮询扫码结果。region 仅国际版需要（新号必须先做地区注册） */
  poll: (state: string, realm?: Realm, region?: string) =>
    get<{
      status: 'waiting' | 'success' | 'expired' | 'invalid' | 'realm_mismatch';
      uid?: string;
      nickname?: string;
      updated?: boolean;
      realm?: Realm;
      region_note?: string;
      expected?: Realm;
      got?: Realm;
    }>('/api/auth/poll', {state, realm, region}),
  remove: (file: string) => del<{success: boolean}>(`/api/accounts/${encodeURIComponent(file)}`),
  /** 临时禁用 / 启用账号（issue #21）：改文件名 + 触发上游重载。 */
  setDisabled: (file: string, disabled: boolean) =>
    post<{ok: boolean; file: string; disabled: boolean; changed: boolean;
          reload_triggered: boolean; message: string}>(
      `/api/accounts/${encodeURIComponent(file)}/disabled`, {disabled}),
  checkin: (file: string) =>
    post<{code: number; message: string; credits?: number | null}>(
      `/api/accounts/${encodeURIComponent(file)}/checkin`,
    ),
  /** 单个账号的实时积分（直接向腾讯查询） */
  credits: (file: string) =>
    get<{
      ok: boolean;
      credits: number | null;
      message: string;
      cached: boolean;
      cache_age: number | null;
      expiries?: CreditExpiry[];
    }>(`/api/accounts/${encodeURIComponent(file)}/credits`),
  /** 并发刷新所有账号的实时积分 */
  /** 查询全部账号积分；force=false 时 60 秒内命中服务端缓存 */
  refreshCredits: (force = true) =>
    post<{
      total: number;
      succeeded: number;
      credits: Record<string, number | null>;
      meta: Record<string, CreditsMeta>;
      failed: string[];
    }>('/api/accounts/refresh-credits' + (force ? '?force=true' : '?force=false')),
  checkinAll: () =>
    post<{total: number; succeeded: number; results: {nickname: string; ok: boolean; message: string}[]}>(
      '/api/accounts/checkin-all',
    ),
  /** 签到记录（分页）。days 用于时间范围筛选 */
  checkinLogs: (limit = 20, offset = 0, uid?: string, days?: number, realm?: Realm) =>
    get<CheckinLogPage>('/api/checkin-logs', {limit, offset, uid, days, realm}),
  clearCheckinLogs: () => post<{ok: boolean}>('/api/checkin-logs/clear'),
  /** 自动任务记录（分页）。kind / days 为筛选条件 */
  taskLogs: (limit = 20, offset = 0, kind?: string, uid?: string, days?: number, realm?: Realm) =>
    get<TaskLogResponse>('/api/task-logs', {limit, offset, kind, uid, days, realm}),
  collectTaskLogs: () => post<{ok: boolean; parsed: number; added: number}>('/api/task-logs/collect'),
  clearTaskLogs: () => post<{ok: boolean}>('/api/task-logs/clear'),
  upstreamLogs: (limit = 200) =>
    get<{available: boolean; lines: string[]; total: number}>('/api/upstream/logs', {limit}),
  test: (file: string) =>
    post<{ok: boolean; message: string}>(`/api/accounts/${encodeURIComponent(file)}/test`),
  refresh: (file: string) =>
    post<{ok: boolean; message: string}>(`/api/accounts/${encodeURIComponent(file)}/refresh`),
  restart: () => post<{ok: boolean; message: string}>('/api/restart'),

  /* ── 成长任务一键执行（issue #19）─────────────────────
   * 调用上游自带的 scripts/task_runner.py。full（点亮）会伪造活跃上报，
   * 因此单独要求 confirm，与幂等的 claim 区分开。 */
  taskRunStatus: () => get<TaskRunStatus>('/api/task-run'),
  taskRunStart: (mode: 'preview' | 'claim' | 'full', target = 'ALL', confirm = false) =>
    post<{ok: boolean; message: string}>('/api/task-run', {mode, target, confirm}),
  taskRunStop: () => post<{ok: boolean; message: string}>('/api/task-run/stop'),
  taskClaimSchedule: () => get<{enabled: boolean; hours: number[]}>('/api/task-claim-schedule'),
  saveTaskClaimSchedule: (enabled: boolean, hours: number[]) =>
    put<{enabled: boolean; hours: number[]}>('/api/task-claim-schedule', {enabled, hours}),
};

/* ── 上游状态 ───────────────────────────────────────── */
export const upstreamApi = {
  status: () => get<UpstreamStatus>('/api/status'),
  /** 上游模型简表；realm 非空时只返回该版本的条目 */
  models: (realm?: Realm) => get<ModelListResponse>('/api/models', {realm}),
};

/* ── 模型中心 ───────────────────────────────────────── */
export const modelApi = {
  /** 指定版本的模型目录；force=true 绕过 5 分钟缓存 */
  catalog: (realm: Realm = 'cn', force = false) =>
    get<ModelCatalog>('/api/model-catalog', {realm, force}),
};

/* ── 聊天测试台 ─────────────────────────────────────── */
export const playgroundApi = {
  /** 指定版本的可选模型 + 各自支持的推理档位（与模型中心同源） */
  models: (realm: Realm = 'cn') =>
    get<PlaygroundModels>('/api/playground/models', {realm}),
};

/* ── API 密钥 ───────────────────────────────────────── */
export const keyApi = {
  list: () => get<ApiKey[]>('/api/keys'),
  create: (body: Partial<ApiKey>) => post<ApiKey>('/api/keys', body),
  update: (id: number, body: Partial<ApiKey>) => patch<ApiKey>(`/api/keys/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),
  resetUsage: (id: number) => post<{ok: boolean}>(`/api/keys/${id}/reset-usage`),
  /**
   * 检查模型白名单里哪些名字匹配不到已知模型（issue #46）。
   *
   * 判据在后端（与调用侧同一份），前端只负责显示——不然就是第二份事实来源。
   * `checked: false` 表示拿不到模型清单（缓存未就绪），此时**不能**把空
   * `unknown` 当成「全部正确」展示。
   */
  checkModels: (models: string[], realm: string) =>
    post<{checked: boolean; unknown: string[]; reason?: string}>(
      '/api/keys/check-models', {models, realm}),
};

/* ── 日志 ───────────────────────────────────────────── */
export const logApi = {
  list: (params: Record<string, unknown>) => get<Page<RequestLog>>('/api/logs', params),
  clear: () => post<{ok: boolean}>('/api/logs/clear'),
};

/* ── 用量统计 ───────────────────────────────────────── */
export const statsApi = {
  /** realm 非空时只统计该版本（界面按版本切换时传） */
  summary: (realm?: Realm) => get<StatsSummary>('/api/stats/summary', {realm}),
  daily: (days = 30, realm?: Realm) => get<UsagePoint[]>('/api/stats/daily', {days, realm}),
  byModel: (days = 30, realm?: Realm) =>
    get<UsageBreakdown[]>('/api/stats/by-model', {days, realm}),
  byKey: (days = 30, realm?: Realm) =>
    get<UsageBreakdown[]>('/api/stats/by-key', {days, realm}),
  /** 按请求日志回填用量缺口（幂等） */
  rebuildUsage: () =>
    post<{rows_before: number; rows_after: number; requests_delta: number; tokens_delta: number}>(
      '/api/stats/rebuild-usage',
    ),
  repairUsage: () =>
    post<{repaired: number; requests: number; tokens: number}>('/api/stats/repair-usage'),
};

/* ── 安全 / IP ──────────────────────────────────────── */
export const securityApi = {
  config: () => get<SecurityConfig>('/api/security/config'),
  saveConfig: (body: SecurityConfig) => post<SecurityConfig>('/api/security/config', body),
  rules: () => get<IpRule[]>('/api/security/rules'),
  addRule: (body: {kind: 'allow' | 'deny'; cidr: string; note?: string}) =>
    post<IpRule>('/api/security/rules', body),
  removeRule: (id: number) => del<{ok: boolean}>(`/api/security/rules/${id}`),
  logs: (limit = 200) => get<IpAccessLog[]>('/api/security/logs', {limit}),
  logsClear: () => post<{ok: boolean}>('/api/security/logs/clear'),
};

/* ── 上游配置 / 用户 ───────────────────────────────── */
export const settingsApi = {
  upstream: () => get<UpstreamConfig>('/api/settings/upstream'),
  saveUpstream: (body: Record<string, unknown>) =>
    post<UpstreamConfig>('/api/settings/upstream', body),
  /** 测试 Upstash 连通性；token 留空表示使用已保存的值 */
  testUpstash: (url: string, token?: string) =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/test', {url, token}),
  /** 上游重载状态（保存配置后自动重启） */
  reloadState: () => get<ReloadState>('/api/upstream/reload-state'),
  /** 立即重启上游（一般无需手动调用，保存配置会自动重载） */
  reloadUpstream: () =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/reload'),
  modelMap: () => get<Record<string, string>>('/api/settings/model-map'),
  saveModelMap: (body: Record<string, string>) =>
    post<Record<string, string>>('/api/settings/model-map', body),
  users: () => get<UserItem[]>('/api/users'),
  /** 管理端审计日志（登录/改密码/增删用户等，仅管理员可读） */
  auditLogs: (limit = 200) => get<AuditLogPage>('/api/audit-logs', {limit}),
  addUser: (body: {username: string; password: string; role: string}) =>
    post<UserItem>('/api/users', body),
  /** 改密码 / 改角色；会吊销该用户的既有会话（改自己则需重新登录） */
  updateUser: (username: string, body: {password?: string; role?: string}) =>
    patch<UserItem & {sessions_revoked?: boolean; relogin_required?: boolean}>(
      `/api/users/${encodeURIComponent(username)}`, body),
  removeUser: (username: string) => del<{ok: boolean}>(`/api/users/${encodeURIComponent(username)}`),
};

export type {Account};

/* ── 系统维护（一键更新）──────────────────────────────── */
export const systemApi = {
  updateStatus: () => get<UpdateStatus>('/api/system/update-status'),
  /** 检测新版本；force=true 绕过 6 小时缓存 */
  checkUpdate: (force = false) => get<UpdateCheck>('/api/system/check-update', {force}),
  versions: () => get<Versions>('/api/system/versions'),
  /** 启动一键更新；target: manager | upstream | both */
  startUpdate: (target: 'manager' | 'upstream' | 'both') =>
    post<{ok: boolean; message: string}>('/api/system/update', {target}),
  /** 固定上游版本（空串 = 取消固定，恢复跟随分支） */
  setUpstreamRef: (ref: string) =>
    post<{ok: boolean; upstream_ref: string}>('/api/system/upstream-ref', {ref}),
  /** 更新日志（解析仓库根目录 CHANGELOG.md，离线可用） */
  changelog: () => get<Changelog>('/api/system/changelog'),
};
