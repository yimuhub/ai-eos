/** @type {import('next').NextConfig} */
const nextConfig = {
  // 改造说明：此处原有 `eslint: { ignoreDuringBuilds: true }`，注释写的是
  // "dev 与 CI 已单独检查" —— 但实际上当时**没有任何地方在检查 lint**
  // （tools/check.sh 里没有 eslint 步骤），于是构建一路报成功，
  // 而代码里已经积累了 36 个 error。
  //
  // 现在 lint 已归零，并且已接入交付门禁 tools/check.sh。
  // 这里不再忽略：让 `npm run build` 这条默认路径也能拦住 lint 问题，
  // 避免"构建绿了但代码有问题"的假绿再次出现。
  //
  // 如确需在打包环境临时跳过（注意 Next 13+ 已移除 `--no-lint` 参数），
  // 需在此显式加回 `eslint: { ignoreDuringBuilds: true }`，
  // 并在提交说明里写清原因——不要让这个开关悄悄回来。
};

export default nextConfig;
