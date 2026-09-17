import dynamic from "next/dynamic";
const WarRoomDetail = dynamic(() => import("@/components/WarRoomDetail"), { ssr: false });
export default function Page() { return <WarRoomDetail />; }
