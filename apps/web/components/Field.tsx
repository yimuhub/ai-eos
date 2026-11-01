"use client";

// 统一表单字段容器
//
// ## 解决的问题
// 改造前每个输入框都是"裸"的：没有标签（只有 placeholder）、没有必填提示、
// 出错时无处显示。placeholder 一旦输入就消失，用户填到一半会忘记这一栏要填什么；
// 出错时更是只能靠"按钮点不动"来猜。
//
// 这个组件把「标签 / 必填标记 / 可选说明 / 错误信息」四件事收敛到一处，
// 并负责把错误用 `aria-describedby` + `aria-invalid` 关联到输入控件上，
// 让读屏用户也能听到错误——这一条在原来的实现里是完全缺失的。
//
// 用法：
// ```tsx
// <Field label="姓名" required error={form.errors.name} hint="会显示在组织架构里">
//   <input className="input" value={name} onChange={...} />
// </Field>
// ```

import {
  cloneElement,
  isValidElement,
  useId,
  type ReactElement,
  type ReactNode,
} from "react";

export type FieldProps = {
  /** 字段名。替代 placeholder 承担"说明这一栏是什么"的职责 */
  label: string;
  /** 显示红色的必填星号（同时给读屏留了文字说明） */
  required?: boolean;
  /** 常驻说明：格式要求、影响范围，或"可留空"这类提示 */
  hint?: string;
  /** 错误文案。由 lib/validate.ts 的规则产出，非空即视为校验未通过 */
  error?: string;
  /** 控件 id，用于 label 关联。不传则只做视觉分组（不用 for 指向不存在的元素） */
  htmlFor?: string;
  /** 控件本体 */
  children: ReactNode;
  /** 与相邻字段拉开间距用的类名，默认不额外加 */
  className?: string;
};

/** 只给原生表单控件补 ARIA；div 包裹层（个别调用方用它做 flex 布局）不能挂 aria-invalid */
const FORM_CONTROLS = new Set(["input", "select", "textarea"]);

type ControlA11yProps = {
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "true" | "false";
};

export default function Field({
  label,
  required,
  hint,
  error,
  htmlFor,
  children,
  className = "",
}: FieldProps) {
  // 没传 htmlFor 时仍要能关联错误：用 useId 生成本地 id（只用于 aria-describedby，
  // 不给 label 加 for，避免"标签指向了不存在的控件"）。冒号在 id 里合法但选择器要转义，去掉。
  const autoId = useId().replace(/:/g, "");
  const baseId = htmlFor || `field-${autoId}`;
  const errId = `${baseId}-error`;
  const hintId = `${baseId}-hint`;
  const describedBy = error ? errId : hint ? hintId : undefined;

  const controlElement =
    isValidElement<ControlA11yProps>(children) &&
    typeof children.type === "string" &&
    FORM_CONTROLS.has(children.type)
      ? (children as ReactElement<ControlA11yProps>)
      : null;

  // 把错误/说明挂到控件上：读屏用户聚焦输入框时能直接听到"哪里错了"，
  // 而不是只能靠 role=alert 的一次性播报（离开页面重进就再也听不到）。
  // 若调用方自己传了 aria-describedby，合并而不是覆盖。
  const control = controlElement
    ? cloneElement(controlElement, {
        "aria-describedby":
          [controlElement.props["aria-describedby"], describedBy].filter(Boolean).join(" ") || undefined,
        "aria-invalid": error ? true : controlElement.props["aria-invalid"],
      })
    : children;

  return (
    <div className={className}>
      <label htmlFor={htmlFor} className="flex items-center gap-1 text-xs mb-1.5" style={{ color: "var(--muted)" }}>
        <span>{label}</span>
        {required && (
          <span aria-hidden style={{ color: "var(--danger)" }}>
            *
          </span>
        )}
        {required && <span className="sr-only">（必填）</span>}
      </label>

      {/* 错误优先于说明展示：出错时用户更需要知道哪里错了，而不是格式要求 */}
      <div aria-live="polite">{control}</div>

      {error ? (
        <p id={errId} role="alert" className="text-[11px] mt-1 flex items-start gap-1" style={{ color: "var(--danger)" }}>
          <span aria-hidden>⚠</span>
          <span>{error}</span>
        </p>
      ) : (
        hint && (
          <p id={hintId} className="text-[11px] mt-1" style={{ color: "var(--muted)" }}>
            {hint}
          </p>
        )
      )}
    </div>
  );
}
