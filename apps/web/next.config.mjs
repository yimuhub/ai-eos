/** @type {import('next').NextConfig} */
const nextConfig = {
  eslint: {
    // 便携打包：构建时跳过 lint（dev 与 CI 已单独检查）
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
