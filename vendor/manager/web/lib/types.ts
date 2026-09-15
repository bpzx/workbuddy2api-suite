export type Role = 'admin' | 'viewer';

export interface Me {
  username: string;
  role: Role;
}

export interface Account {
  /** 该令牌签发的总时长（秒）；后端从 JWT 解出，解不出为 null */
  ttl_seconds?: number | null;
  /** 令牌签发时间（秒）≈ 最近一次刷新时间；后端从 JWT iat 解出，解不出为 null */
  issued_at?: number | null;
  file: string;
  uid: string;
  nickname: string;
  enterprise_id: string;
  expires_at: number;
  is_expired: boolean;
  remain_seconds: number;
  /** 当前可花费积分余额（上游聚合套餐剩余额度），null 表示尚未同步 */
  credits?: number | null;
  /** 来自 workbuddy2api /status 的运行时字段，可能为空 */
  healthy?: boolean | null;
  disabled?: boolean | null;
  /** 被上游禁用时的原因（如 11140 request illegal 需重新登录）；空串表示未给出 */
  disabled_reason?: string | null;
  in_flight?: number | null;
  cooling?: boolean | null;
  success_count?: number | null;
  err_total?: number | null;
  breaker_fails?: number | null;
  last_success?: string | null;
  last_used?: number | null;
  source: 'file' | 'pool';
  /** 账号所属版本（cn / global）；存量账号按域名回退，无该字段时视为 cn */
  realm?: 'cn' | 'global';
  /** 该版本是否支持签到体系（国际版没有） */
  checkin_supported?: boolean;
  /** auth 文件里的 domain，便于确认归属 */
  domain?: string;
}

export interface AccountsResponse {
  total: number;
  accounts: Account[];
  /** 已从上游同步到运行时状态的账号数 */
  pool_synced?: number;
  /** 上游是否可达 */
  pool_available?: boolean;
}

export interface UpstreamStatus {
  connected: boolean;
  accounts?: Record<string, unknown>[];
  cooling?: number;
  disabled?: number;
  healthy?: number;
  total?: number;
  in_flight_full?: number;
  redis_mode?: string;
  sticky_sessions?: number;
  error?: string;
}

export interface ModelInfo {
  id: string;
  owned_by?: string;
  /** 上下文窗口（上游动态拉取给出；静态回退表为固定值） */
  context_length?: number;
  context_window?: number;
  /**
   * 最大输出 token。**只有上游动态拉取的条目才带这个键**，内置静态回退表没有，
   * 前端据此判断列表来源（见 ModelListResponse.source）。
   */
  max_output_tokens?: number | null;
}

/** 模型列表来源：dynamic = 上游实时动态拉取；static = 上游内置静态回退表 */
export type ModelSource = 'dynamic' | 'static' | 'unknown';

export interface ModelListResponse {
  models: ModelInfo[];
  source: ModelSource;
  count: number;
}

/* ── 模型中心（模型目录）────────────────────────────────── */

/** 模型目录里的一条：比 /v1/models 多显示名与推理档位 */
export interface CatalogModel {
  id: string;
  /** 显示名，来自腾讯模型接口；上游回退数据为空串 */
  name: string;
  /** 上下文窗口（token，0 = 未知） */
  context_length: number;
  /** 最大输出（token，0 = 未知） */
  max_output_tokens: number;
  /** 支持的推理档位，如 ['low','high','max']；空数组 = 非推理模型或未提供 */
  efforts: string[];
  /** 系列归属（按 id 前缀推导，仅用于分组浏览） */
  series: string;
}

export interface CatalogSummary {
  total: number;
  /** 带推理档位的模型数 */
  reasoning: number;
  /** 上下文 ≥128K 的模型数 */
  large_context: number;
  /** 最大上下文（token） */
  max_context: number;
  /** 涉及系列 */
  series: string[];
  unique_ids: number;
}

/**
 * 数据来源：
 * - `tencent`：直连腾讯模型接口，字段最全（含显示名与推理档位）
 * - `upstream`：回退到上游 /v1/models，字段有限
 * - `none`：两者都拿不到
 */
export type CatalogSourceKind = 'tencent' | 'upstream' | 'none';

export interface ModelCatalog {
  models: CatalogModel[];
  source: CatalogSourceKind;
  /** 来源的中文说明，直接用于界面标注 */
  source_label: string;
  /** 本次数据取自哪个账号（回退时为 workbuddy2api） */
  via: string;
  /** 逐次尝试的失败原因，便于排查（不展示给普通用户也要留着） */
  errors: string[];
  /** 命中缓存时为 true */
  cached: boolean;
  /** 缓存已存在多少秒 */
  cache_age: number;
  summary: CatalogSummary;
}

export interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  enabled: boolean;
  expires_at: number | null;
  max_ips: number;
  ip_allowlist: string[];
  models: string[];
  quota: number | null;
  used_tokens: number;
  created_at: number;
  last_used_at: number | null;
  /** 仅在创建时返回一次 */
  key?: string;
}

export interface RequestLog {
  id: number;
  ts: number;
  key_id: number | null;
  key_name: string | null;
  ip: string;
  model: string | null;
  mapped_model: string | null;
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  /**
   * 首字延迟（毫秒）：从发起上游请求到收到第一个含正文的 delta。
   * null = 未采集（非流式请求，或升级前的历史记录）。
   * 与 latency_ms 的区别：latency_ms 含模型生成全部内容的耗时，回答越长越大，
   * 反映不出上游响应快慢；首字延迟才是「上游多久开始回话」。
   */
  first_token_ms: number | null;
  ua: string | null;
  error: string | null;
  stream: boolean;
  /** 本次调用的真实扣费（上游 usage.credit）；null = 上游未返回，不是 0 */
  credit: number | null;
}

export interface UsagePoint {
  day: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 当日实际扣费合计 */
  credit: number;
}

export interface UsageBreakdown {
  name: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 该维度实际扣费合计 */
  credit: number;
}

export interface StatsSummary {
  today_requests: number;
  today_tokens: number;
  /** 实际扣费（上游 usage.credit 合计）；上游未返回该字段时恒为 0 */
  today_credit: number;
  week_credit: number;
  total_credit: number;
  week_requests: number;
  week_tokens: number;
  total_requests: number;
  total_tokens: number;
  active_keys: number;
  top_model: string | null;
}

export interface IpRule {
  id: number;
  kind: 'allow' | 'deny';
  cidr: string;
  note: string;
  created_at: number;
}

export interface IpAccessLog {
  id: number;
  ts: number;
  ip: string;
  path: string;
  blocked: boolean;
  ua: string | null;
}

export interface SecurityConfig {
  enabled: boolean;
  mode: 'whitelist' | 'blacklist';
}

export interface UserItem {
  username: string;
  role: Role;
}

export interface UpstashConfig {
  /** Upstash 地址，支持 https://xxx.upstash.io 或 xxx.upstash.io */
  url: string;
  /** 是否已配置 token（内容不回传） */
  has_token: boolean;
  /** token 掩码，仅用于展示 */
  token_masked: string;
}

export interface UpstreamConfig {
  /** 是否成功读取到上游配置文件；false 时前端应提示并禁止保存 */
  available?: boolean;
  /** 上游配置文件路径 */
  config_path?: string;
  /** 读取失败原因 */
  error?: string;
  listen?: string;
  api_key_masked?: string;
  auth_dir?: string;
  /** 上游 config.json 中声明的 auth_dir，与管理端不一致时出现 */
  upstream_auth_dir?: string;
  schedule?: Record<string, unknown>;
  pool?: Record<string, unknown>;
  cooldown?: Record<string, unknown>;
  features?: Record<string, unknown>;
  session_sticky?: Record<string, unknown>;
  prompt?: Record<string, unknown>;
  server?: Record<string, unknown>;
  upstream?: Record<string, unknown>;
  /** 国际版（workbuddy.ai）路由：enabled / chat_base / billing_base */
  global?: Record<string, unknown>;
  upstash?: UpstashConfig;
}

/** 积分查询来源：实时查询 or 命中 60 秒缓存 */
export interface CreditsMeta {
  cached: boolean;
  cache_age: number | null;
  message?: string;
}

export interface CheckinLog {
  id: number;
  ts: number;
  uid: string;
  nickname: string;
  /** 触发来源：manual 手动 / manual-batch 批量 / add 添加账号 / auto 上游自动 */
  source: string;
  /** 类型：checkin 签到 / keepalive 保活 */
  kind: string;
  success: boolean;
  code: number | null;
  message: string;
  /** true = 来自上游自动签到（采集器落库），false = 本端触发 */
  auto?: boolean;
}

/** 上游自动任务留痕（猫猫旅行 / 活跃上报 / 自动签到 / 保活） */
export interface TaskLog {
  id: number;
  ts: number;
  uid: string;
  /** travel / activity / checkin / keepalive / user-resource / credit */
  kind: string;
  /** credit 有积分收益 / ok 成功 / info 跳过 / warn 警告 / error 失败 */
  level: string;
  credits: number;
  /** 上游英文原文（排查用，界面上作为悬浮提示） */
  message: string;
  /** 中文展示文案 */
  message_cn?: string;
  /** 账号昵称（后端按 uid/前缀解析；上游日志只带 uid 前 8 位） */
  nickname?: string;
}

export interface TaskLogResponse {
  logs: TaskLog[];
  /** 当前筛选下的总条数（分页用；与 stats.total 在未筛选时一致） */
  total: number;
  stats: {
    by_kind: Record<string, {count: number; credits: number}>;
    total: number;
    total_credits: number;
  };
  kinds: Record<string, string>;
  collector: {at?: number; parsed?: number; added?: number; error?: string};
}


/** 签到记录分页返回 */
export interface CheckinLogPage {
  items: CheckinLog[];
  /** 当前筛选下的总条数（本端触发 + 上游自动） */
  total: number;
  /** 其中本端触发的条数 */
  local_total?: number;
  /** 其中上游自动签到的条数 */
  auto_total?: number;
}

export interface UpdateLogLine {
  ts: number;
  level: string;
  text: string;
}

export interface UpdateStatus {
  available: boolean;
  /** 是否正在更新 */
  running: boolean;
  /** 上次更新是否成功（null = 未运行过） */
  ok: boolean | null;
  step: string;
  logs: UpdateLogLine[];
  /** 当前部署版本 */
  version: string;
  updater_found: boolean;
  upstream_dir: string;
  /** 当前固定的上游版本（空 = 跟随分支） */
  upstream_ref?: string;
  started_at?: number;
  finished_at?: number | null;
  duration?: number;
  pid?: number;
  /** 日志原文（便于复制反馈） */
  log_tail?: string;
  /**
   * 发布包签名校验结果（供应链防护）。
   * verified = 已验签通过；skipped = 走了 WB_SKIP_SIGNATURE 绕过开关（有风险）；
   * none 或缺失 = 未执行验签（如旧版本更新器）。
   */
  signature?: {status: 'verified' | 'skipped' | 'none'; detail?: string};
}

export interface VersionSide {
  /** 当前版本 */
  current: string;
  /** 远端最新 */
  latest: string;
  /** 是否有更新可用 */
  has_update: boolean;
  /** 检测失败原因 */
  error: string;
}

export interface ManagerVersion extends VersionSide {
  /** Release 页面地址 */
  url: string;
  repo: string;
}

/** 上游两个版本之间的单条提交 */
export interface UpstreamChange {
  sha: string;
  subject: string;
  date: string;
}

export interface UpstreamVersion extends VersionSide {
  /** 最新提交时间 */
  date: string;
  /** 最新提交说明 */
  subject: string;
  /** 本地落后远端多少个提交（0 = 未知或不落后） */
  ahead?: number;
  /** 两版本之间的提交总数 */
  total?: number;
  /** 变更说明列表（最新在前，最多 20 条） */
  changes?: UpstreamChange[];
  /** 提交数超过展示上限，列表被截断 */
  truncated?: boolean;
  repo: string;
}

export interface UpdateCheck {
  /** 检测时间（秒） */
  checked_at: number;
  /** 是否为缓存结果 */
  cached: boolean;
  manager: ManagerVersion;
  upstream: UpstreamVersion;
  /** 任一组件有更新 */
  has_any: boolean;
}

export interface Versions {
  manager: string;
  upstream_connected: boolean;
  upstream_accounts: number | null;
  upstream_dir: string;
}

/** 更新日志中的一条（level 1 = 二级缩进，作为子项展示） */
export interface ChangelogItem {
  level: number;
  text: string;
}

export interface ChangelogSection {
  /** 中文分类：安全 / 新增 / 修复 / 改进 / 说明 / 计划中 */
  title: string;
  items: ChangelogItem[];
}

export interface ChangelogVersion {
  /** 版本号，未发布的开发内容为「未发布」 */
  version: string;
  /** 发布日期，未发布时为空串 */
  date: string;
  /** 是否为尚未发布的开发内容 */
  unreleased: boolean;
  sections: ChangelogSection[];
}

export interface Changelog {
  /** CHANGELOG.md 是否可读 */
  available: boolean;
  /** 不可读时的原因 */
  error?: string;
  /** 文件路径（便于排查） */
  path?: string;
  /** 版本总数（未被截断时） */
  total?: number;
  versions: ChangelogVersion[];
  /** 版本过多，列表被截断 */
  truncated?: boolean;
  /** 当前部署版本，用于高亮 */
  current?: string;
}

export interface ReloadState {
  /** 正在执行重启 */
  running: boolean;
  /** 有重启在排队（短时间内多次改动的合并） */
  pending: boolean;
  /** 上次重启完成时间（秒） */
  last_at: number;
  last_ok: boolean | null;
  last_message: string;
  restart_count: number;
}

export interface Page<T> {
  total: number;
  items: T[];
}

/* ── 聊天测试台 ─────────────────────────────────────── */

export interface PlaygroundModel {
  id: string;
  name: string;
  /** 支持的推理档位；空数组 = 该模型不支持调整 */
  efforts: string[];
  series: string;
}

export interface PlaygroundModels {
  models: PlaygroundModel[];
  source: CatalogSourceKind;
  source_label: string;
}

/* ── 管理端审计日志 ─────────────────────────────────── */

export interface AuditLog {
  id: number;
  ts: number;
  /** 操作者用户名（anonymous 表示未认证） */
  actor: string;
  /** login / login_failed / update_user / delete_user ... */
  action: string;
  target: string;
  detail: string;
  ip: string;
}

export interface AuditLogPage {
  items: AuditLog[];
  total: number;
}
