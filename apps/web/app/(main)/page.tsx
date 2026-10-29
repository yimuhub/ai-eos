import { redirect } from "next/navigation";

/** 首页就是工作台。原来的指挥中心搬到 /dashboard，仍可从侧边栏和 ⌘K 进入。 */
export default function Home() {
  redirect("/workspace");
}
