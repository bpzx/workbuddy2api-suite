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
};

export default nextConfig;
