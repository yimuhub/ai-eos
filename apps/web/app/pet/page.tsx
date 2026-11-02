import PetClient from "./PetClient";

/**
 * 桌面壳里的常驻 CEO 小人页。
 *
 * 只有 Electron 小人窗口会带 `?desktop=1` 打开它：此时页面背景必须透明，
 * 否则透明窗口里会出现一块黑底。样式在服务端就写进 HTML，避免水合前闪一下。
 * 浏览器直接访问 /pet 时保持正常深色背景，并提示这是桌面端专属。
 */
export default function PetPage({
  searchParams,
}: {
  searchParams?: { [key: string]: string | string[] | undefined };
}) {
  const desktop = searchParams?.desktop === "1";
  return (
    <>
      {desktop && (
        <style>{`
          html, body { background: transparent !important; overflow: hidden; }
        `}</style>
      )}
      <PetClient desktop={desktop} />
    </>
  );
}
