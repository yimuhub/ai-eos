// lib/validate.ts 纯函数测试：统一校验规则。
//
// 设计取向（见文件头注释）：每条规则返回「中文错误或 null」，
// 文案带字段名。测试锁定这些文案，避免改规则时悄悄改变用户看到的话。
import { describe, expect, it } from "vitest";
import { V, firstError, hasErrors, runRules, validateAll } from "./validate";

describe("V.required", () => {
  it("空/空白报错并带字段名", () => {
    expect(V.required("姓名")("")).toBe("请填写姓名");
    expect(V.required("姓名")("   ")).toBe("请填写姓名");
  });
  it("非空通过", () => {
    expect(V.required("姓名")("张三")).toBeNull();
  });
});

describe("V.minLen / V.maxLen", () => {
  it("太短报错", () => {
    expect(V.minLen(2, "姓名")("张")).toBe("姓名至少 2 个字符");
  });
  it("太长报错并给出当前长度", () => {
    expect(V.maxLen(2, "姓名")("张三李")).toBe("姓名不能超过 2 个字符（当前 3）");
  });
  it("边界通过", () => {
    expect(V.maxLen(2, "姓名")("张三")).toBeNull();
  });
});

describe("V.email", () => {
  it("明显非法被拦", () => {
    expect(V.email()("not-an-email")).toBe("邮箱格式不正确");
    expect(V.email()("a@b")).toBe("邮箱格式不正确");
  });
  it("合法通过，留空不算错（是否必填交给 required）", () => {
    expect(V.email()("a@b.co")).toBeNull();
    expect(V.email()("")).toBeNull();
  });
  it("保留域名给出中文提示（后端 EmailStr 只会回英文 422）", () => {
    expect(V.email()("boss@demo.local")).toBe(
      "邮箱域名不可用（.local / .test 等是保留域名），请换成真实邮箱",
    );
    expect(V.email()("boss@demo.com")).toBeNull();
  });
});

describe("V.url", () => {
  it("非法地址报错", () => {
    expect(V.url()("not a url")).toBe(
      "地址不是合法地址（例：https://api.example.com/v1）",
    );
  });
  it("非 http(s) 协议报错", () => {
    expect(V.url()("ftp://x.com")).toBe("地址必须以 http:// 或 https:// 开头");
  });
  it("http(s) 通过，留空视为用默认值", () => {
    expect(V.url()("https://api.example.com/v1")).toBeNull();
    expect(V.url()("")).toBeNull();
  });
});

describe("V.envVarName", () => {
  it("小写/数字开头被拦", () => {
    expect(V.envVarName()("abc")).toBe(
      "环境变量名只能由大写字母、数字、下划线组成，且以字母开头（例：HERMES_API_KEY）",
    );
    expect(V.envVarName()("1ABC")).not.toBeNull();
  });
  it("合法变量名通过", () => {
    expect(V.envVarName()("OPENAI_COMPAT_API_KEY")).toBeNull();
  });
});

describe("V.identifier", () => {
  it("非法字符被拦", () => {
    expect(V.identifier("模型 ID")("a b")).toBe(
      "模型 ID只能包含字母、数字与 . _ - : / 这些符号",
    );
  });
  it("合法标识通过", () => {
    expect(V.identifier()("agnes-2.5-flash")).toBeNull();
  });
});

describe("V.password（与后端 auth 规则一致）", () => {
  it("太短", () => {
    expect(V.password()("abc123")).toBe("密码至少 8 位");
  });
  it("缺字母", () => {
    expect(V.password()("12345678")).toBe("密码需包含至少一个字母");
  });
  it("缺数字", () => {
    expect(V.password()("abcdefgh")).toBe("密码需包含至少一个数字");
  });
  it("合法通过", () => {
    expect(V.password()("abc12345")).toBeNull();
  });
});

describe("V.intInRange", () => {
  it("非整数/越界报错", () => {
    expect(V.intInRange(1, 5, "数量")("x")).toBe("数量必须是整数");
    expect(V.intInRange(1, 5, "数量")("0")).toBe("数量需要在 1 到 5 之间");
  });
  it("范围内通过", () => {
    expect(V.intInRange(1, 5, "数量")("3")).toBeNull();
  });
});

describe("runRules / validateAll / hasErrors / firstError", () => {
  it("runRules 返回第一条错误，顺序即优先级", () => {
    expect(runRules("", [V.required("姓名"), V.maxLen(2, "姓名")])).toBe(
      "请填写姓名",
    );
    expect(runRules("张三李", [V.required("姓名"), V.maxLen(2, "姓名")])).toBe(
      "姓名不能超过 2 个字符（当前 3）",
    );
    expect(runRules("张三", [V.required("姓名"), V.maxLen(2, "姓名")])).toBeNull();
  });

  it("validateAll 一次校验多字段", () => {
    const errors = validateAll(
      { name: "", count: "x" },
      { name: [V.required("姓名")], count: [V.intInRange(1, 5, "数量")] },
    );
    expect(errors).toEqual({ name: "请填写姓名", count: "数量必须是整数" });
  });

  it("hasErrors / firstError", () => {
    expect(hasErrors({ a: undefined })).toBe(false);
    expect(hasErrors({ a: "错" })).toBe(true);
    expect(firstError({ a: undefined, b: "第二条" })).toBe("第二条");
    expect(firstError({})).toBeNull();
  });
});
