/** 单一版本来源：package.json（与 apps/api/app/version.py 保持一致，一致性由 CI 保障）。
 * 修复前登录页徽标硬编码 "V5.0"，与实际版本（v5.15.0）脱节。 */
import pkg from "../package.json";

export const APP_VERSION = `V${pkg.version}`;
