"use client";

import dynamic from "next/dynamic";
import type { Member, Presence } from "@/lib/types";

const RoomScene = dynamic(() => import("./room-scene"), { ssr: false, loading: () => <div className="grid h-full place-items-center text-sm text-white/60">Preparing room…</div> });

export default function SceneClient(props: { members: Member[]; ownId: string; presence: Presence[]; theme: string; reduced: boolean; onMove(position: Presence): void }) {
  return <RoomScene {...props} />;
}
