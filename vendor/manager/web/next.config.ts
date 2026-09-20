import type {NextConfig} from 'next';

const isExport = process.env.NEXT_OUTPUT_EXPORT === '1';
const backend = process.env.NEXT_PUBLIC_BACKEND_BASE_URL || 'http://127.0.0.1:7864';

const nextConfig: NextConfig = {
  ...(isExport ?
    {
      output: 'export' as const,
      trailingSlash: true,
    } :
    {
      async rewrites() {
        return [
          {source: '/api/:path*', destination: `${backend}/api/:path*`},
          {source: '/v1/:path*', destination: `${backend}/v1/:path*`},
          {source: '/v2/:path*', destination: `${backend}/v2/:path*`},
          {source: '/healthz', destination: `${backend}/healthz`},
        ];
      },
    }),
  images: {
    unoptimized: true,
    remotePatterns: [],
  },
  // 构建时间注入为环境变量：package.json 里的 buildDate 是死值（从没更新过），
  // 界面上「Build At」会永远显示同一个日期，属于会误导人的信息。
  // 运行版本另以后端为准，这里只回答「这份前端产物是什么时候构建的」。
  env: {
    NEXT_PUBLIC_BUILD_TIME: new Date().toISOString(),
  },
};

export default nextConfig;
