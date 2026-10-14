// 统一校验规则（纯函数，无 React 依赖）
//
// ## 为什么要有这个文件
// 改造前每个页面的校验强度都不一样，而且是三种最差的做法混合：
//   - `if (!form.name) return;` —— 点了按钮什么都不发生，用户不知道哪里错了；
//   - 只在按钮上 `disabled={!form.name}` —— 用户知道"点不动"，但不知道"为什么"；
//   - 完全没校验 —— 把问题丢给后端，用户看到一串技术性的 422 报错。
//
// 一致的规则 + 一句中文错误 + 明确指向哪个字段，是"用起来顺心"的基本盘。
// 这里只放**规则**；错误状态的管理在 lib/useFormValidation.ts，展示在 components/Field.tsx。
//
// 设计取向：
//   - 每条规则返回「中文错误或 null」，不做异常、不做布尔值——调用处无需解释错误含义；
//   - 规则可组合（`[V.required("姓名"), V.maxLen(50, "姓名")]`），顺序即报错优先级；
//   - 所有文案都带字段名，避免出现"此项必填"这种用户得自己猜的提示。

export type Rule = (value: string) => string | null;

/** 把「值 + 规则列表」跑成第一条错误 */
export function runRules(value: string | null | undefined, rules: Rule[]): string | null {
  const v = value ?? "";
  for (const rule of rules) {
    const err = rule(v);
    if (err) return err;
  }
  return null;
}

const isBlank = (v: string) => !v || !v.trim();

export const V = {
  /** 必填。label 会出现在错误文案里（"请填写姓名"） */
  required:
    (label: string): Rule =>
    (v) =>
      isBlank(v) ? `请填写${label}` : null,

  minLen:
    (n: number, label: string): Rule =>
    (v) =>
      !isBlank(v) && v.trim().length < n ? `${label}至少 ${n} 个字符` : null,

  maxLen:
    (n: number, label: string): Rule =>
    (v) =>
      v.trim().length > n ? `${label}不能超过 ${n} 个字符（当前 ${v.trim().length}）` : null,

  /** 邮箱：不做完美校验，只拦住明显的错（漏 @、带空格） */
  email:
    (label = "邮箱"): Rule =>
    (v) => {
      if (isBlank(v)) return null; // 是否必填交给 required 规则
      return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v.trim()) ? null : `${label}格式不正确`;
    },

  /** http(s) 地址。留空视为"用默认值"，不算错 */
  url:
    (label = "地址"): Rule =>
    (v) => {
      if (isBlank(v)) return null;
      try {
        const u = new URL(v.trim());
        return u.protocol === "http:" || u.protocol === "https:" ? null : `${label}必须以 http:// 或 https:// 开头`;
      } catch {
        return `${label}不是合法地址（例：https://api.example.com/v1）`;
      }
    },

  /** 环境变量名：只允许大写字母、数字、下划线，且不以数字开头 */
  envVarName:
    (label = "环境变量名"): Rule =>
    (v) => {
      if (isBlank(v)) return null;
      return /^[A-Z][A-Z0-9_]*$/.test(v.trim())
        ? null
        : `${label}只能由大写字母、数字、下划线组成，且以字母开头（例：HERMES_API_KEY）`;
    },

  /** 模型 ID / 标识符：字母数字与 . _ - : / */
  identifier:
    (label = "标识符"): Rule =>
    (v) => {
      if (isBlank(v)) return null;
      return /^[A-Za-z0-9._:\/-]+$/.test(v.trim())
        ? null
        : `${label}只能包含字母、数字与 . _ - : / 这些符号`;
    },

  /** 密码强度：与后端 auth 的规则保持一致，避免"前端放行、后端拒绝" */
  password:
    (): Rule =>
    (v) => {
      if (isBlank(v)) return null;
      if (v.length < 8) return "密码至少 8 位";
      if (!/[A-Za-z]/.test(v)) return "密码需包含至少一个字母";
      if (!/[0-9]/.test(v)) return "密码需包含至少一个数字";
      return null;
    },

  /** 纯数字正整数（如批量生成数量），并给出上下限 */
  intInRange:
    (min: number, max: number, label: string): Rule =>
    (v) => {
      if (isBlank(v)) return null;
      const n = Number(v);
      if (!Number.isInteger(n)) return `${label}必须是整数`;
      if (n < min || n > max) return `${label}需要在 ${min} 到 ${max} 之间`;
      return null;
    },

  /** 自定义：需要一次性约束时用，例如「不能与现任同名」 */
  custom:
    (message: string, ok: (v: string) => boolean): Rule =>
    (v) =>
      isBlank(v) || ok(v) ? null : message,
};

/** 多字段一次校验，返回「字段 → 第一条错误」 */
export function validateAll<T extends Record<string, string | undefined>>(
  values: T,
  schema: { [K in keyof T]?: Rule[] },
): Partial<Record<keyof T, string>> {
  const errors: Partial<Record<keyof T, string>> = {};
  (Object.keys(schema) as (keyof T)[]).forEach((key) => {
    const rules = schema[key];
    if (!rules?.length) return;
    const err = runRules(values[key], rules);
    if (err) errors[key] = err;
  });
  return errors;
}

/** 校验结果里是否还有错误 */
export function hasErrors(errors: Record<string, string | undefined>): boolean {
  return Object.values(errors).some(Boolean);
}

/** 取第一条错误，用于 Toast 一句话提示 */
export function firstError(errors: Record<string, string | undefined>): string | null {
  for (const v of Object.values(errors)) if (v) return v;
  return null;
}
