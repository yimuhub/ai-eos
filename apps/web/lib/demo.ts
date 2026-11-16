/** 演示公司种子：一键预置一家可直接浏览的演示公司（员工/交付单/任务）。
 *  幂等：同一用户重复调用返回已有演示公司（already_seeded=true）。
 *  创建成功后调用方应 setActiveCompanyId(cid) 并刷新工作台数据。 */
import { api } from "./api";

export type DemoSeedResult = {
  company_id: string;
  company_name: string;
  already_seeded: boolean;
  agents?: number;
  deliverables?: number;
  task_id?: string;
};

export async function seedDemoCompany(): Promise<DemoSeedResult> {
  return api.post<DemoSeedResult>("/demo/seed");
}
