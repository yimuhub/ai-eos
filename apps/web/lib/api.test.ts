// lib/api.ts 纯函数测试：错误规范化、错误类型判断、401 识别。
//
// 覆盖目标（静态正确性，Mac 上不实际运行）：
//   - normalizeErrorDetail：后端各种形状的 detail → 一句人话；
//   - ApiError 的 isAuth/isPermission/isNotFound/isNetwork：页面据此分支
//     （401 → 尝试 refresh / 跳登录，403 → 说清缺权限，404 → 资源不存在）；
//   - describeError：任意异常 → 可展示中文。
import { describe, expect, it } from "vitest";
import { ApiError, describeError, normalizeErrorDetail } from "./api";

describe("normalizeErrorDetail", () => {
  it("空值返回空串", () => {
    expect(normalizeErrorDetail(null)).toBe("");
    expect(normalizeErrorDetail(undefined)).toBe("");
    expect(normalizeErrorDetail("")).toBe("");
  });

  it("字符串原样返回", () => {
    expect(normalizeErrorDetail("模型未就绪")).toBe("模型未就绪");
  });

  it("字符串数组用；连接", () => {
    expect(normalizeErrorDetail(["a", "b"])).toBe("a；b");
  });

  it("FastAPI 422 数组：去掉 body/query 前缀，loc 与 msg 用中文冒号连接", () => {
    expect(
      normalizeErrorDetail([{ loc: ["body", "name"], msg: "字段必填" }]),
    ).toBe("name：字段必填");
    expect(
      normalizeErrorDetail([{ loc: ["query", "q"], msg: "m" }]),
    ).toBe("q：m");
  });

  it("数组元素只有 message 时直接取 message", () => {
    expect(normalizeErrorDetail([{ message: "oops" }])).toBe("oops");
  });

  it("对象有 message 取 message，否则 JSON 序列化", () => {
    expect(normalizeErrorDetail({ message: "hello" })).toBe("hello");
    expect(normalizeErrorDetail({ other: 1 })).toBe('{"other":1}');
  });

  it("数字/布尔转字符串", () => {
    expect(normalizeErrorDetail(42)).toBe("42");
    expect(normalizeErrorDetail(true)).toBe("true");
  });
});

describe("ApiError 状态判断（401 跳转 / 403 / 404 / 网络失败分支依据）", () => {
  it("401 识别为登录态失效", () => {
    const e = new ApiError("登录已过期，请重新登录", 401, "/tasks");
    expect(e.isAuth).toBe(true);
    expect(e.isPermission).toBe(false);
    expect(e.isNotFound).toBe(false);
    expect(e.isNetwork).toBe(false);
  });

  it("403 识别为无权限", () => {
    const e = new ApiError("没有权限执行该操作", 403, "/x");
    expect(e.isPermission).toBe(true);
    expect(e.isAuth).toBe(false);
  });

  it("404 识别为资源不存在", () => {
    const e = new ApiError("请求的资源不存在", 404, "/x");
    expect(e.isNotFound).toBe(true);
    expect(e.isAuth).toBe(false);
  });

  it("status 0 识别为网络层失败/超时", () => {
    const e = new ApiError("无法连接后端服务，请确认服务已启动。", 0, "/x");
    expect(e.isNetwork).toBe(true);
    expect(e.isAuth).toBe(false);
  });

  it("携带 path 与 detail", () => {
    const e = new ApiError("msg", 422, "/models", [{ msg: "bad" }]);
    expect(e.path).toBe("/models");
    expect(e.detail).toEqual([{ msg: "bad" }]);
  });
});

describe("describeError", () => {
  it("ApiError 取 message", () => {
    expect(describeError(new ApiError("登录已过期", 401, "/x"))).toBe(
      "登录已过期",
    );
  });

  it("普通 Error 取 message", () => {
    expect(describeError(new Error("boom"))).toBe("boom");
  });

  it("字符串原样返回", () => {
    expect(describeError("oops")).toBe("oops");
  });

  it("未知形状走兜底文案", () => {
    expect(describeError({})).toBe("操作失败");
    expect(describeError({}, "自定义兜底")).toBe("自定义兜底");
    expect(describeError(new Error("")), "空 message 也兜底").toBe("操作失败");
  });
});
