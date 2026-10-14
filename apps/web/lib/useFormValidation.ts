"use client";

// 表单校验状态管理（配合 lib/validate.ts 使用）
//
// 把「什么时候校验、什么时候别烦用户」这件事统一起来——这是表单体验好坏的真正分水岭：
//   - 页面刚打开、用户还没输入就飘红，是最让人烦躁的做法，所以**默认不校验**；
//   - 用户提交时统一校验一次，指出**所有**问题，而不是挤牙膏式的一条一条报；
//   - 用户开始修正某个字段后，该字段的错误立即消失，不必等下次提交。
//
// 用法：
// ```tsx
// const form = useFormValidation(formSchema);
// ...
// if (!form.check(values)) { notify("warning", form.first()!); return; }
// <Field label="姓名" required error={form.errors.name}> ... </Field>
// ```

import { useCallback, useMemo, useState } from "react";
import { firstError, hasErrors, validateAll, type Rule } from "@/lib/validate";

export function useFormValidation<T extends Record<string, string | undefined>>(
  schema: { [K in keyof T]?: Rule[] },
) {
  const [errors, setErrors] = useState<Partial<Record<keyof T, string>>>({});

  /** 全量校验。返回是否通过；不通过时 errors 里是**全部**问题的集合 */
  const check = useCallback(
    (values: T): boolean => {
      const next = validateAll(values, schema);
      setErrors(next);
      return !hasErrors(next);
    },
    [schema],
  );

  /** 清掉单个字段的错误。输入框 onChange 里调用 */
  const clear = useCallback((key: keyof T) => {
    setErrors((prev) => {
      if (!prev[key]) return prev; // 本来就没错，别触发多余渲染
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  const reset = useCallback(() => setErrors({}), []);

  const first = useCallback(() => firstError(errors), [errors]);

  return useMemo(
    () => ({ errors, check, clear, reset, first, invalid: hasErrors(errors) }),
    [errors, check, clear, reset, first],
  );
}
